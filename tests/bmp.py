"""Minimal BMP collector (RFC 7854). FRR connects and pushes its BGP data; we keep each peer's routes.

A BMP message is: common header (version, length, type) + per-peer header + payload.
For route monitoring the payload is the BGP UPDATE the peer sent us, copied as-is.
"""
import ipaddress
import logging
import socket
import struct
import threading

log = logging.getLogger("bmp")
ROUTE_MONITORING, STATS_REPORT, PEER_DOWN, PEER_UP, INITIATION, TERMINATION = range(6)
POST_POLICY = 0x40                       # per-peer header flag: routes after our inbound policy


class BmpCollector:
    def __init__(self, port=5000):
        self.routes = {}                 # peer BGP ID -> set of prefixes it sent us (post-policy)
        self.announced = {}              # peer BGP ID -> prefixes announced so far (a running counter)
        self.up = {}                     # peer BGP ID -> session up?
        self.downs = {}                  # peer BGP ID -> number of times the session went down
        self.lock = threading.Lock()
        self.server = socket.create_server(("0.0.0.0", port), reuse_port=True)
        threading.Thread(target=self._accept, daemon=True).start()

    # ---- what tests use ----
    def total_routes(self):
        """Paths held from all peers: the same thing as 'BGP paths' in vtysh, minus local routes."""
        with self.lock:
            return sum(len(r) for r in self.routes.values())

    def peer(self, bgp_id):
        with self.lock:
            return {"up": self.up.get(bgp_id, False), "routes": len(self.routes.get(bgp_id, ())),
                    "announced": self.announced.get(bgp_id, 0), "downs": self.downs.get(bgp_id, 0)}

    def peers_up(self):
        with self.lock:
            return sum(self.up.values())

    # ---- network side ----
    def _accept(self):
        while True:
            conn, addr = self.server.accept()
            log.info("BMP session from %s", addr[0])
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn):
        buf = b""
        while data := conn.recv(1 << 16):
            buf += data
            while len(buf) >= 6:
                _version, length, msg_type = struct.unpack("!BIB", buf[:6])
                if len(buf) < length:
                    break
                body, buf = buf[6:length], buf[length:]
                if msg_type in (ROUTE_MONITORING, PEER_DOWN, PEER_UP):
                    self._handle(msg_type, body)

    def _handle(self, msg_type, body):
        flags = body[1]
        peer = str(ipaddress.IPv4Address(body[30:34]))        # per-peer header: peer's BGP router-id
        with self.lock:
            if msg_type == PEER_UP:
                self.up[peer] = True
                self.routes[peer] = set()
            elif msg_type == PEER_DOWN:
                self.up[peer] = False
                self.downs[peer] = self.downs.get(peer, 0) + 1
                self.routes[peer] = set()
            elif flags & POST_POLICY:
                added, removed = parse_update(body[42:])         # the BGP UPDATE after the 42-byte header
                rib = self.routes.setdefault(peer, set())
                rib.difference_update(removed)
                rib.update(added)
                self.announced[peer] = self.announced.get(peer, 0) + len(added)


def parse_update(msg):
    """Return (announced, withdrawn) IPv4 prefixes from a BGP UPDATE, incl. MP_REACH/MP_UNREACH."""
    body = msg[19:struct.unpack("!H", msg[16:18])[0]]          # skip the 19-byte BGP header
    wlen = struct.unpack("!H", body[:2])[0]
    withdrawn = prefixes(body[2:2 + wlen])
    alen = struct.unpack("!H", body[2 + wlen:4 + wlen])[0]
    attrs = body[4 + wlen:4 + wlen + alen]
    announced = prefixes(body[4 + wlen + alen:])
    i = 0
    while i < len(attrs):                                      # IPv4 over unnumbered uses MP attributes
        aflags, atype = attrs[i], attrs[i + 1]
        if aflags & 0x10:                                      # extended length
            alen2, hdr = struct.unpack("!H", attrs[i + 2:i + 4])[0], 4
        else:
            alen2, hdr = attrs[i + 2], 3
        val = attrs[i + hdr:i + hdr + alen2]
        if atype == 14 and val[:3] == b"\x00\x01\x01":         # MP_REACH_NLRI, IPv4 unicast
            nh_len = val[3]
            announced += prefixes(val[5 + nh_len:])
        elif atype == 15 and val[:3] == b"\x00\x01\x01":       # MP_UNREACH_NLRI, IPv4 unicast
            withdrawn += prefixes(val[3:])
        i += hdr + alen2
    return announced, withdrawn


def prefixes(data):
    """Decode BGP NLRI: <length in bits><just enough bytes> repeated."""
    out, i = [], 0
    while i < len(data):
        plen = data[i]
        n = (plen + 7) // 8
        addr = ipaddress.IPv4Address(data[i + 1:i + 1 + n].ljust(4, b"\x00"))
        out.append(f"{addr}/{plen}")
        i += 1 + n
    return out
