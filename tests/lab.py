"""Small helpers: run commands in the lab containers and read BGP state from FRR (vtysh JSON)."""
import json
import os
import re
import subprocess
import time

LAB = "clab-sonic-rr"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(node, cmd):
    """Run a shell command inside a lab container and return its output."""
    r = subprocess.run(["docker", "exec", f"{LAB}-{node}", "bash", "-c", cmd],
                       capture_output=True, text=True, check=True)
    return r.stdout


def vtysh_json(node, cmd):
    return json.loads(sh(node, f"vtysh -c '{cmd} json'"))


def wait_until(check, timeout, interval=1):
    """Call check() until it returns True; raise if `timeout` seconds pass first."""
    end = time.time() + timeout
    while time.time() < end:
        if check():
            return
        time.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout}s")


def set_mode(mode):
    """Reconfigure the running lab with configure.sh, then wait until leaf1's BGP sessions are up."""
    subprocess.run(["./configure.sh"], cwd=REPO, env={**os.environ, "MODE": mode},
                   check=True, stdout=subprocess.DEVNULL)
    peers = 8 if mode == "traditional" else 10      # 8 spines, plus 2 route servers in pivot
    wait_until(lambda: established_peers() >= peers, timeout=300)


def scale(action, n):
    """Add or delete n GPU /32 routes behind leaf2 (scale.sh)."""
    subprocess.run(["./scale.sh", action, str(n)], cwd=REPO, check=True, stdout=subprocess.DEVNULL)


def established_peers(node="leaf1"):
    peers = vtysh_json(node, "show bgp ipv4 unicast summary")["peers"]
    return sum(p["state"] == "Established" for p in peers.values())


def bgp_paths(node="leaf1"):
    """Total BGP paths held by the node ('show bgp memory' -> 'N BGP routes, using ...')."""
    out = sh(node, "vtysh -c 'show bgp memory'")
    return int(re.search(r"(\d+) BGP routes", out).group(1))


def neighbor(nbr, node="leaf1"):
    """State and number of IPv4 prefixes accepted from one BGP neighbor."""
    d = vtysh_json(node, f"show bgp neighbors {nbr}")[nbr]
    prefixes = d.get("addressFamilyInfo", {}).get("ipv4Unicast", {}).get("acceptedPrefixCounter", 0)
    return {"state": d["bgpState"], "prefixes": prefixes}
