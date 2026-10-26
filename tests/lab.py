"""Helpers to drive the sonic-rr containerlab: docker exec, vtysh JSON, mode switching."""
import json
import os
import re
import subprocess
import time

LAB = "clab-sonic-rr"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPINES = 8
WATCH_PREFIX = "10.100.132.1"          # srv4 gpu1: other rail, behind leaf2


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


def scale(action, n):
    run_script("./scale.sh", action, str(n))


def route_ifnames(prefix=WATCH_PREFIX, node="leaf1"):
    out = sh(node, f"redis-cli -n 0 hget ROUTE_TABLE:{prefix} ifname").strip()
    return set(out.split(",")) if out else set()


def established_peers(node="leaf1"):
    peers = vtysh_json(node, "show bgp ipv4 unicast summary").get("peers", {})
    return {p for p, v in peers.items() if v.get("state") == "Established"}


def wait_converged(mode, timeout=300):
    want_peers = SPINES + (0 if mode == "traditional" else 2)
    wait_until(lambda: len(established_peers()) >= want_peers, timeout, 2, f"{want_peers} BGP peers")
    wait_until(lambda: len(route_ifnames()) == SPINES, timeout, 2, f"{WATCH_PREFIX} via {SPINES} spines")
    time.sleep(3)  # let trailing updates settle


# ---------- BGP counters ----------
def bgp_neighbor(nbr, node="leaf1"):
    """State, UPDATEs received and prefixes accepted for one BGP neighbor."""
    d = vtysh_json(node, f"show bgp neighbors {nbr}")[nbr]
    return {"state": d["bgpState"],
            "updates_rcvd": d["messageStats"]["updatesRecv"],
            "prefixes": d.get("addressFamilyInfo", {}).get("ipv4Unicast", {}).get("acceptedPrefixCounter", 0)}


def bgp_update_counters(node):
    """Per-neighbor UPDATE messages received/sent."""
    nbrs = vtysh_json(node, "show bgp neighbors")
    return {n: (v["messageStats"]["updatesRecv"], v["messageStats"]["updatesSent"])
            for n, v in nbrs.items() if isinstance(v, dict) and "messageStats" in v}


def bgp_memory(node="leaf1"):
    """(paths, bytes) from 'show bgp memory' -> the 'N BGP routes, using X' line."""
    out = sh(node, "vtysh -c 'show bgp memory' 2>/dev/null")
    m = re.search(r"(\d+) BGP routes, using (\d+) (\w+)", out)
    mult = {"bytes": 1, "KiB": 1024, "MiB": 1024 ** 2}[m.group(3)]
    return int(m.group(1)), int(m.group(2)) * mult


def diff(after, before):
    return {k: tuple(a - b for a, b in zip(after[k], before.get(k, (0,) * len(after[k]))))
            for k in after}
