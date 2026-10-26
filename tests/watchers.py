"""Push-based watchers: each runs a background thread and records (host_time, event).

GnmiRouteWatcher   - gNMI STREAM/ON_CHANGE on APPL_DB ROUTE_TABLE (SONiC FIB), via the sidecar
KernelRouteWatcher - `ip monitor route nexthop` inside the node (Linux FIB, pushed by netlink)
BgpPrefixWatcher   - polls FRR for one prefix's BGP paths (gNMI can't see BGP-only changes)
"""
import subprocess
import threading
import time

from pygnmi.client import gNMIclient

from lab import LAB, vtysh_json


class EventLog:
    def __init__(self):
        self.events = []
        self.cv = threading.Condition()

    def add(self, ev):
        with self.cv:
            self.events.append((time.time(), ev))
            self.cv.notify_all()

    def since(self, t0):
        with self.cv:
            return [(t, e) for t, e in self.events if t >= t0]

    def wait_for(self, pred, since, timeout):
        """Block until an event after `since` satisfies pred(event, events_so_far); return its time."""
        end = time.time() + timeout
        seen = []
        with self.cv:
            i = 0
            while True:
                while i < len(self.events):
                    t, e = self.events[i]
                    i += 1
                    if t < since:
                        continue
                    seen.append(e)
                    if pred(e, seen):
                        return t
                left = end - time.time()
                if left <= 0:
                    raise TimeoutError("event not seen")
                self.cv.wait(left)

    def wait_quiet(self, since, quiet=3.0, timeout=120):
        """Wait until no events for `quiet` s; return (time of last event after since, count)."""
        end = time.time() + timeout
        while time.time() < end:
            evs = self.since(since)
            last = evs[-1][0] if evs else since
            if time.time() - last >= quiet and (evs or time.time() - since >= quiet):
                return (last if evs else None), len(evs)
            time.sleep(0.2)
        raise TimeoutError("events never went quiet")


class GnmiRouteWatcher(EventLog):
    """Each event: (prefix, fields-dict). Empty dict = route deleted."""

    def __init__(self, host, port=8080):
        super().__init__()
        self.host, self.port = host, port
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.wait(60):
            raise RuntimeError("gNMI subscription did not sync")

    def _run(self):
        sub = {"subscription": [{"path": "ROUTE_TABLE", "mode": "on_change"}],
               "mode": "stream", "encoding": "json_ietf", "prefix": ""}
        with gNMIclient(target=(self.host, self.port), skip_verify=True,
                        username="lab", password="lab") as c:
            for msg in c.subscribe2(subscribe=sub, target="APPL_DB"):
                if "sync_response" in msg:
                    self.ready.set()
                    continue
                for u in msg.get("update", {}).get("update", []):
                    for prefix, fields in (u.get("val") or {}).items():
                        self.add((prefix, fields))
                self.ready.set()


class KernelRouteWatcher(EventLog):
    """Each event: one line of `ip monitor route nexthop` (route or nexthop-group change)."""

    def __init__(self, node="leaf1"):
        super().__init__()
        self.proc = subprocess.Popen(
            ["docker", "exec", f"{LAB}-{node}", "stdbuf", "-oL", "ip", "monitor", "route", "nexthop"],
            stdout=subprocess.PIPE, text=True, bufsize=1)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                self.add(line)

    def close(self):
        self.proc.terminate()


class BgpPrefixWatcher(EventLog):
    """Polls FRR every `interval` s; records an event when the prefix's path set changes."""

    def __init__(self, prefix, node="leaf1", interval=0.2):
        super().__init__()
        self.prefix, self.node, self.interval = prefix, node, interval
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def paths(self):
        try:
            d = vtysh_json(self.node, f"show bgp ipv4 unicast {self.prefix}")
        except Exception:
            return None
        return tuple(sorted(p.get("peer", {}).get("peerId", "?") for p in d.get("paths", [])))

    def _run(self):
        last = object()
        while not self._stop.is_set():
            cur = self.paths()
            if cur is not None and cur != last:
                self.add(cur)
                last = cur
            time.sleep(self.interval)

    def close(self):
        self._stop.set()
