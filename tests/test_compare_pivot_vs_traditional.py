"""Traditional leaf/spine vs HPN Pivot, same lab, same events, measured on leaf1.

traditional: leaf1 learns each leaf2 GPU /32 from all 8 spines (8 BGP paths per prefix)
pivot:       leaf1 learns it from the 2 route servers (2 paths, next hop = leaf2 loopback,
             resolved recursively over the 8 spines)

Per event we record three layers:
  BGP    - CPU ms burned by bgpd/zebra, UPDATE messages exchanged, BGP paths/memory held
  kernel - last `ip monitor route nexthop` message (Linux FIB done) and how many messages
  FIB    - last gNMI ON_CHANGE update from APPL_DB ROUTE_TABLE (SONiC FIB done) and how many
"""
import time

import pytest

import lab
from lab import LEAF1_SPINE1_PORT, SCALE_N, PingProbe

pytestmark = pytest.mark.slow
RUNS = 3
SPINE_NODES = [f"spine{i}" for i in range(1, lab.SPINES + 1)]


def is_injected(prefix):
    parts = prefix.split(".")
    return len(parts) == 4 and parts[1] == "100" and int(parts[2]) >= 192


def all_injected(match, n=SCALE_N):
    """gNMI predicate: true once every injected prefix has had an update satisfying match(fields)."""
    done = set()

    def pred(event, _seen):
        prefix, fields = event
        if is_injected(prefix) and match(fields):
            done.add(prefix)
        return len(done) >= n
    return pred


def total(counters, idx):
    return sum(v[idx] for v in counters.values())


@pytest.fixture(scope="module", params=["traditional", "pivot"])
def mode(request, gnmi):
    """Reconfigure the running lab into one design; every test below runs once per design."""
    m = request.param
    if lab.injected_route_count():
        lab.scale("del")
    lab.set_mode(m, bfd="off")
    lab.wait_converged(m, injected=0)
    yield m
    if lab.injected_route_count():
        lab.scale("del")


@pytest.fixture
def loaded(mode):
    """10k injected GPU routes present on leaf1 via all 8 spines."""
    if lab.injected_route_count() != SCALE_N:
        lab.scale("add")
    lab.wait_until(lambda: lab.injected_route_count() == SCALE_N, 300, 2, "10k routes on leaf1")
    lab.wait_until(lambda: len(lab.route_ifnames("10.100.192.5")) == lab.SPINES, 300, 2, "8-way ECMP")
    time.sleep(3)


@pytest.mark.parametrize("run", range(RUNS))
def test_route_burst(mode, run, gnmi, kernel, results):
    """leaf2 announces 10k new GPU /32s at once, then withdraws them."""
    if lab.injected_route_count():
        lab.scale("del")
        lab.wait_until(lambda: lab.injected_route_count() == 0, 300, 2, "routes withdrawn")
        time.sleep(3)

    upd0 = lab.bgp_update_counters("leaf1")
    spine0 = {s: lab.bgp_update_counters(s) for s in SPINE_NODES}
    cpu0 = lab.proc_cpu_ms()

    t0 = time.time()
    lab.scale("add")
    t_fib = gnmi.wait_for(all_injected(lambda f: bool(f)), since=t0, timeout=300)
    t_kern, n_kern = kernel.wait_quiet(since=t0)
    cpu = lab.diff(lab.proc_cpu_ms(), cpu0)
    upd = lab.diff(lab.bgp_update_counters("leaf1"), upd0)
    spine_sent = sum(total(lab.diff(lab.bgp_update_counters(s), spine0[s]), 1) for s in SPINE_NODES)
    paths, mem = lab.bgp_memory()

    results.add("10k GPU routes announced", mode,
                fib_converged_s=round(t_fib - t0, 2),
                kernel_last_update_s=round(t_kern - t0, 2),
                kernel_msgs=n_kern,
                gnmi_route_updates=len(gnmi.since(t0)),
                leaf1_updates_rcvd=total(upd, 0),
                spines_updates_sent=spine_sent,
                bgpd_cpu_ms=cpu.get("bgpd", 0),
                zebra_cpu_ms=cpu.get("zebra", 0),
                orchagent_cpu_ms=cpu.get("orchagent", 0),
                leaf1_bgp_paths=paths,
                leaf1_bgp_mem_kib=mem // 1024)

    # withdraw them again (also the cleanup for the next run)
    cpu0 = lab.proc_cpu_ms()
    t1 = time.time()
    lab.scale("del")
    t_gone = gnmi.wait_for(all_injected(lambda f: f == {}), since=t1, timeout=300)
    cpu = lab.diff(lab.proc_cpu_ms(), cpu0)
    results.add("10k GPU routes withdrawn", mode,
                fib_converged_s=round(t_gone - t1, 2),
                bgpd_cpu_ms=cpu.get("bgpd", 0),
                zebra_cpu_ms=cpu.get("zebra", 0))

    assert t_fib - t0 < 300 and t_gone - t1 < 300


@pytest.mark.parametrize("run", range(RUNS))
def test_spine_link_failure(mode, run, loaded, gnmi, kernel, results):
    """leaf1 loses its link to spine1 with 10k remote GPU routes installed.

    traditional: BGP must drop spine1's path for every one of the 10k prefixes
    pivot:       only leaf2's loopback route changes; the 10k routes follow recursively
    """
    probe = PingProbe().start()
    time.sleep(1)
    cpu0 = lab.proc_cpu_ms()
    t0 = time.time()
    lab.link_down(LEAF1_SPINE1_PORT)
    try:
        t_fib = gnmi.wait_for(all_injected(lambda f: bool(f) and LEAF1_SPINE1_PORT not in f["ifname"].split(",")),
                              since=t0, timeout=300)
        t_kern, n_kern = kernel.wait_quiet(since=t0)
        t_kern_first = kernel.since(t0)[0][0] if n_kern else None
        cpu = lab.diff(lab.proc_cpu_ms(), cpu0)
        n_gnmi = len(gnmi.since(t0))
        loss = probe.stop()
    finally:
        t1 = time.time()
        lab.link_up(LEAF1_SPINE1_PORT)

    t_back = gnmi.wait_for(all_injected(lambda f: bool(f) and LEAF1_SPINE1_PORT in f["ifname"].split(",")),
                           since=t1, timeout=300)

    results.add("spine1 link down (10k routes)", mode,
                fib_converged_s=round(t_fib - t0, 2),
                kernel_first_update_s=round(t_kern_first - t0, 2) if t_kern_first else "-",
                kernel_last_update_s=round(t_kern - t0, 2) if t_kern else "-",
                kernel_msgs=n_kern,
                gnmi_route_updates=n_gnmi,
                bgpd_cpu_ms=cpu.get("bgpd", 0),
                zebra_cpu_ms=cpu.get("zebra", 0),
                orchagent_cpu_ms=cpu.get("orchagent", 0),
                lost_pkts=loss["lost_pkts"],
                affected_flows=loss["affected_flows"],
                outage_ms=loss["outage_ms"])
    results.add("spine1 link restored (10k routes)", mode, fib_converged_s=round(t_back - t1, 2))

    assert t_fib - t0 < 300
