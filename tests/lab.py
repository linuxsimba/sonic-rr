"""Helpers to drive the sonic-rr containerlab: docker exec, mode switching, failures, probes."""
import json
import os
import re
import subprocess
import time

LAB = "clab-sonic-rr"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPINES = 8
SCALE_N = 10_000
# leaf1 port facing spine1 (spine1/2 on Ethernet0/4, spine3..8 on Ethernet56..76)
LEAF1_SPINE1_PORT = "Ethernet0"
WATCH_PREFIX = "10.100.132.1"          # srv4 gpu1: other rail, behind leaf2
CLK_TCK = 100


def sh(node, cmd, check=True, timeout=120):
    """Run a shell command inside a lab node; return stdout."""
    r = subprocess.run(["docker", "exec", f"{LAB}-{node}", "bash", "-c", cmd],
                       capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{node}: {cmd}: {r.stderr.strip()}")
    return r.stdout


def vtysh_json(node, cmd):
    return json.loads(sh(node, f"vtysh -c '{cmd} json' 2>/dev/null"))


def wait_until(fn, timeout, interval=1.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")


# ---------- lab state ----------
def run_script(*args, env=None):
    subprocess.run(list(args), cwd=REPO, check=True, env={**os.environ, **(env or {})},
                   stdout=subprocess.DEVNULL)


def set_mode(mode, bfd="off"):
    run_script("./configure.sh", env={"MODE": mode, "BFD": bfd, "SPINES": str(SPINES)})


def scale(action, n=SCALE_N):
    run_script("./scale.sh", action, str(n))


def injected_route_count(node="leaf1"):
    """Synthetic GPU /32s (10.100.192.0/18) present in the node's APPL_DB ROUTE_TABLE."""
    lua = ("local n=0 for _,k in ipairs(redis.call('keys','ROUTE_TABLE:10.100.*')) do "
           "local o=tonumber(string.match(k,'ROUTE_TABLE:10%.100%.(%d+)%.')) "
           "if o and o>=192 then n=n+1 end end return n")
    return int(sh(node, f'redis-cli -n 0 eval "{lua}" 0').strip())


def route_ifnames(prefix=WATCH_PREFIX, node="leaf1"):
    out = sh(node, f"redis-cli -n 0 hget ROUTE_TABLE:{prefix} ifname").strip()
    return set(out.split(",")) if out else set()


def established_peers(node="leaf1"):
    peers = vtysh_json(node, "show bgp ipv4 unicast summary").get("peers", {})
    return {p for p, v in peers.items() if v.get("state") == "Established"}


def wait_converged(mode, injected=0, timeout=300):
    want_peers = SPINES + (0 if mode == "traditional" else 2)
    wait_until(lambda: len(established_peers()) >= want_peers, timeout, 2, f"{want_peers} BGP peers")
    wait_until(lambda: len(route_ifnames()) == SPINES, timeout, 2, f"{WATCH_PREFIX} via {SPINES} spines")
    wait_until(lambda: injected_route_count() == injected, timeout, 2, f"{injected} injected routes")
    time.sleep(3)  # let trailing updates settle


# ---------- failures ----------
def link_down(port, node="leaf1"):
    sh(node, f"ip link set {port} down")


def link_up(port, node="leaf1"):
    sh(node, f"ip link set {port} up")


def pause(node):
    subprocess.run(["docker", "pause", f"{LAB}-{node}"], check=True, stdout=subprocess.DEVNULL)


def unpause(node):
    subprocess.run(["docker", "unpause", f"{LAB}-{node}"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- counters ----------
def proc_cpu_ms(node="leaf1", names=("bgpd", "zebra", "fpmsyncd", "orchagent")):
    """CPU time (user+sys, ms) consumed so far by each named process in the node."""
    out = {}
    for name in names:
        pid = sh(node, f"pgrep -xo {name}", check=False).strip()
        if not pid:
            continue
        f = sh(node, f"cat /proc/{pid}/stat").rsplit(")", 1)[1].split()
        out[name] = (int(f[11]) + int(f[12])) * 1000 // CLK_TCK   # utime, stime
    return out


def bgp_update_counters(node):
    """Per-neighbor UPDATE messages received/sent."""
    nbrs = vtysh_json(node, "show bgp neighbors")
    return {n: (v["messageStats"]["updatesRecv"], v["messageStats"]["updatesSent"])
            for n, v in nbrs.items() if isinstance(v, dict) and "messageStats" in v}


def bgp_neighbor(nbr, node="leaf1"):
    """State, UPDATEs received and prefixes accepted for one BGP neighbor."""
    d = vtysh_json(node, f"show bgp neighbors {nbr}")[nbr]
    return {"state": d["bgpState"],
            "updates_rcvd": d["messageStats"]["updatesRecv"],
            "prefixes": d.get("addressFamilyInfo", {}).get("ipv4Unicast", {}).get("acceptedPrefixCounter", 0)}


def bgp_memory(node="leaf1"):
    """(paths, bytes) from 'show bgp memory' -> the 'N BGP routes, using X' line."""
    out = sh(node, "vtysh -c 'show bgp memory' 2>/dev/null")
    m = re.search(r"(\d+) BGP routes, using (\d+) (\w+)", out)
    mult = {"bytes": 1, "KiB": 1024, "MiB": 1024 ** 2}[m.group(3)]
    return int(m.group(1)), int(m.group(2)) * mult


def diff(after, before):
    return {k: tuple(a - b for a, b in zip(after[k], before.get(k, (0,) * len(after[k]))))
            if isinstance(after[k], tuple) else after[k] - before.get(k, 0) for k in after}


# ---------- data plane ----------
class PingProbe:
    """16 cross-rail flows: srv1-4 gpu0 -> srv1-4 gpu1, 10 ms interval. A flow hashes onto one
    spine, so many flows are needed to cover the failed one."""
    INTERVAL = 0.01

    def __init__(self):
        self.flows = [(f"srv{s}", f"10.100.{s}.1", f"10.100.{128 + d}.1")
                      for s in range(1, 5) for d in range(1, 5)]

    def start(self):
        for srv, src, dst in self.flows:
            sh(srv, f"pkill -x ping; nohup ping -i {self.INTERVAL} -W 1 -I {src} {dst} "
                    f"> /tmp/ping-{dst}.log 2>&1 &", check=False)
        return self

    def stop(self):
        for s in range(1, 5):
            sh(f"srv{s}", "pkill -INT -x ping", check=False)
        time.sleep(1)
        lost_total, affected, worst = 0, 0, 0
        for srv, src, dst in self.flows:
            log = sh(srv, f"cat /tmp/ping-{dst}.log", check=False)
            m = re.search(r"(\d+) packets transmitted, (\d+) received", log)
            if m:
                lost = int(m.group(1)) - int(m.group(2))
                lost_total += lost
                affected += lost > 0
                worst = max(worst, lost)
        # outage of the worst flow ~= its lost packets x send interval
        return {"lost_pkts": lost_total, "affected_flows": f"{affected}/{len(self.flows)}",
                "outage_ms": round(worst * self.INTERVAL * 1000)}
