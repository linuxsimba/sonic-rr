"""No-BFD baseline: how long until leaf1 reacts to a *silent* failure (docker pause: links stay up,
the node just stops answering), and how long to recover once it's back.
Pivot mode, BFD off, FRR default timers (keepalive 60 s / hold 180 s).
Watched route: srv4 gpu1 = 10.100.132.1/32 on the other rail, behind leaf2.
"""
import time

import pytest

import lab
from lab import LEAF1_SPINE1_PORT, WATCH_PREFIX, PingProbe
from watchers import BgpPrefixWatcher

pytestmark = pytest.mark.slow
HOLD = 180
DETECT_TIMEOUT = HOLD + 60
RECOVER_TIMEOUT = 300


@pytest.fixture(scope="module", autouse=True)
def pivot_without_bfd(gnmi):
    if lab.injected_route_count():
        lab.scale("del")
    lab.set_mode("pivot", bfd="off")
    lab.wait_converged("pivot", injected=0)


@pytest.fixture
def paused():
    """Pause nodes from inside a test; whatever happens, unpause them afterwards."""
    nodes = []

    def _pause(*names):
        for n in names:
            lab.pause(n)
            nodes.append(n)
        return time.time()
    yield _pause
    for n in nodes:
        lab.unpause(n)


@pytest.fixture
def bgp_paths():
    w = BgpPrefixWatcher(WATCH_PREFIX)
    yield w
    w.close()


def watched(match):
    """gNMI predicate on the watched route's APPL_DB fields."""
    return lambda ev, _seen: ev[0] == WATCH_PREFIX and match(ev[1])


def test_spine1_silent_failure(gnmi, paused, results):
    probe = PingProbe().start()
    time.sleep(1)
    t0 = paused("spine1")
    t_detect = gnmi.wait_for(watched(lambda f: f and LEAF1_SPINE1_PORT not in f["ifname"]), t0, DETECT_TIMEOUT)
    loss = probe.stop()

    t1 = time.time()
    lab.unpause("spine1")
    t_back = gnmi.wait_for(watched(lambda f: f and LEAF1_SPINE1_PORT in f["ifname"]), t1, RECOVER_TIMEOUT)

    results.add("silent failure, no BFD: spine1", "pivot",
                detect_s=round(t_detect - t0, 1), recover_s=round(t_back - t1, 1),
                lost_pkts=loss["lost_pkts"], affected_flows=loss["affected_flows"],
                outage_ms=loss["outage_ms"])
    assert t_detect - t0 <= DETECT_TIMEOUT


def test_rr1_silent_failure(gnmi, paused, bgp_paths, results):
    """FIB never changes (rr2 gives the same next hop), so gNMI sees nothing: watch BGP paths."""
    before = lab.route_ifnames()
    probe = PingProbe().start()
    time.sleep(1)
    t0 = paused("rr1")
    t_detect = bgp_paths.wait_for(lambda paths, _: paths == ("10.0.0.102",), t0, DETECT_TIMEOUT)
    loss = probe.stop()
    # zebra may re-send the route when its best path changes; only a different next-hop set counts
    fib_updates = [f for _, (p, f) in gnmi.since(t0) if p == WATCH_PREFIX]
    fib_changes = [f for f in fib_updates if set(f.get("ifname", "").split(",")) != before]

    t1 = time.time()
    lab.unpause("rr1")
    t_back = bgp_paths.wait_for(lambda paths, _: len(paths) == 2, t1, RECOVER_TIMEOUT)

    results.add("silent failure, no BFD: rr1", "pivot",
                detect_s=round(t_detect - t0, 1), recover_s=round(t_back - t1, 1),
                lost_pkts=loss["lost_pkts"], fib_updates_for_route=len(fib_updates),
                fib_nexthop_changes=len(fib_changes))
    assert loss["lost_pkts"] == 0, "losing one route server must not drop traffic"
    assert not fib_changes, "rr1 failure must not change the route's next hops"


def test_both_rr_silent_failure(gnmi, paused, results):
    """Both route servers gone: leaf1 keeps forwarding until the hold timer expires, then
    withdraws every RSC-learned route (no headless forwarding / group GR)."""
    probe = PingProbe().start()
    time.sleep(1)
    t0 = paused("rr1", "rr2")
    t_withdraw = gnmi.wait_for(watched(lambda f: f == {}), t0, DETECT_TIMEOUT)

    t1 = time.time()
    lab.unpause("rr2")
    t_back = gnmi.wait_for(watched(lambda f: bool(f)), t1, RECOVER_TIMEOUT)
    loss = probe.stop()
    lab.unpause("rr1")

    results.add("silent failure, no BFD: rr1+rr2", "pivot",
                withdrawn_after_s=round(t_withdraw - t0, 1),
                relearned_after_rr2_back_s=round(t_back - t1, 1),
                lost_pkts=loss["lost_pkts"], affected_flows=loss["affected_flows"],
                outage_ms=loss["outage_ms"])
    assert t_withdraw - t0 <= DETECT_TIMEOUT
