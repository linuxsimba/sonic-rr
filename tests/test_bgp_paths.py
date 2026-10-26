"""Traditional leaf/spine vs Pivot (route servers): BGP route count and route updates on leaf1.

Run:  .venv/bin/pytest
"""
import pytest

import lab

N = 10_000                                          # GPU routes leaf2 announces
SPINE1 = "Ethernet0"                                # leaf1's BGP neighbor towards spine1 (unnumbered)
PATHS_PER_ROUTE = {"traditional": lab.CONFIG["spines"],            # one copy from every spine ...
                   "pivot": lab.CONFIG["route_servers"]}            # ... vs one from every route server


# params: tests using `design` run once per value. scope="module": setup runs once per value.
# yield: code before it is setup, code after it is cleanup.
@pytest.fixture(scope="module", params=["traditional", "pivot"])
def design(request):
    lab.set_mode(request.param)
    lab.scale("add", N)
    lab.wait_until(lambda: lab.bgp_paths() >= N * PATHS_PER_ROUTE[request.param], timeout=300,
                   what=f"{N * PATHS_PER_ROUTE[request.param]:,} BGP paths on leaf1",
                   status=lambda: f"{lab.bgp_paths():,}")
    yield request.param
    lab.scale("del", N)


def test_route_count(design):
    paths = lab.bgp_paths()
    print(f"\n{design}: leaf1 holds {paths:,} BGP paths for {N:,} GPU routes (same routes, more copies)")
    assert paths >= N * PATHS_PER_ROUTE[design]


def test_route_updates_on_spine1_restart(design):
    lab.log.info("restarting BGP on spine1")
    lab.sh("spine1", "vtysh -c 'clear bgp *'")     # spine1 drops and re-opens all its BGP sessions
    lab.wait_until(lambda: lab.neighbor(SPINE1)["state"] != "Established", timeout=30, interval=0.2,
                   what="spine1 session down")
    lab.wait_until(lambda: lab.neighbor(SPINE1)["state"] == "Established", timeout=120,
                   what="spine1 session back up")

    expected = N if design == "traditional" else 2  # traditional: all GPU routes; pivot: just loopbacks
    lab.wait_until(lambda: lab.neighbor(SPINE1)["prefixes"] >= expected, timeout=120,
                   what=f"{expected:,} routes from spine1", status=lambda: f"{lab.neighbor(SPINE1)['prefixes']:,}")
    resent = lab.neighbor(SPINE1)["prefixes"]
    print(f"\n{design}: spine1 re-sent {resent:,} routes to leaf1")

    if design == "pivot":
        assert resent <= lab.CONFIG["spines"] + lab.CONFIG["leaves"]   # loopbacks only
    else:
        assert resent >= N
