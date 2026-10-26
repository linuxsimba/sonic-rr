"""Small helpers: run commands in the lab containers and read BGP state from FRR (vtysh JSON)."""
import json
import logging
import os
import re
import subprocess
import time

LAB = "clab-sonic-rr"
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log = logging.getLogger("lab")                     # shown live when log_cli = true in pytest.ini


def sh(node, cmd):
    """Run a shell command inside a lab container and return its output."""
    r = subprocess.run(["docker", "exec", f"{LAB}-{node}", "bash", "-c", cmd],
                       capture_output=True, text=True, check=True)
    return r.stdout


def vtysh_json(node, cmd):
    return json.loads(sh(node, f"vtysh -c '{cmd} json'"))


def wait_until(check, timeout, interval=1, what="condition", status=None):
    """Call check() until it returns True. Logs progress every 5s (status() shows the current value)."""
    log.info("waiting for %s (up to %ss)", what, timeout)
    start = last_log = time.time()
    while time.time() - start < timeout:
        if check():
            log.info("  done: %s after %.1fs", what, time.time() - start)
            return
        if time.time() - last_log >= 5:
            log.info("  still waiting for %s (%.0fs)%s", what, time.time() - start,
                     f": now {status()}" if status else "")
            last_log = time.time()
        time.sleep(interval)
    raise TimeoutError(f"{what} not met within {timeout}s")


def set_mode(mode):
    """Reconfigure the running lab with configure.sh, then wait until leaf1's BGP sessions are up."""
    log.info("configuring lab: MODE=%s (configure.sh output follows)", mode)
    p = subprocess.Popen(["./configure.sh"], cwd=REPO, env={**os.environ, "MODE": mode},
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:                            # stream each line into the live log
        log.info("  configure.sh | %s", line.rstrip())
    if p.wait():
        raise RuntimeError(f"configure.sh failed (exit {p.returncode})")
    peers = 4 if mode == "traditional" else 6       # 4 spines, plus 2 route servers in pivot
    wait_until(lambda: established_peers() >= peers, timeout=300, what=f"{peers} BGP sessions on leaf1",
               status=lambda: f"{established_peers()} up")


def scale(action, n):
    """Add or delete n GPU /32 routes behind leaf2 (scale.sh)."""
    log.info("%s %d GPU /32 static routes on leaf2 (scale.sh)", action, n)
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
