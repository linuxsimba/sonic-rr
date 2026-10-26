"""Traditional leaf/spine vs Pivot (route servers): BGP route count and route updates on leaf1.

Run:  .venv/bin/pytest
"""
import pytest

import lab

N = 10_000                                          # GPU routes leaf2 announces
SPINE1 = "Ethernet0"                                # leaf1's BGP neighbor towards spine1 (unnumbered)
PATHS_PER_ROUTE = {"traditional": 8, "pivot": 2}    # learned from 8 spines vs 2 route servers


# params: tests using `design` run once per value. scope="module": setup runs once per value.
# yield: code before it is setup, code after it is cleanup.
@pytest.fixture(scope="module", params=["traditional", "pivot"])
def design(request):
    lab.set_mode(request.param)
    lab.scale("add", N)
    lab.wait_until(lambda: lab.bgp_paths() >= N * PATHS_PER_ROUTE[request.param], timeout=300)
    yield request.param
    lab.scale("del", N)


def test_route_count(design):
    paths = lab.bgp_paths()
    print(f"\n{design}: leaf1 holds {paths:,} BGP paths for {N:,} GPU routes")
    assert paths >= N * PATHS_PER_ROUTE[design]


def test_route_updates_on_spine1_restart(design):
    lab.sh("spine1", "vtysh -c 'clear bgp *'")     # spine1 drops and re-opens all its BGP sessions
    lab.wait_until(lambda: lab.neighbor(SPINE1)["state"] != "Established", timeout=30, interval=0.2)
    lab.wait_until(lambda: lab.neighbor(SPINE1)["state"] == "Established", timeout=120)

    expected = N if design == "traditional" else 2  # traditional: all GPU routes; pivot: 2 loopbacks
    lab.wait_until(lambda: lab.neighbor(SPINE1)["prefixes"] >= expected, timeout=120)
    resent = lab.neighbor(SPINE1)["prefixes"]
    print(f"\n{design}: spine1 re-sent {resent:,} routes to leaf1")

    if design == "pivot":
        assert resent < 10
    else:
        assert resent >= N
