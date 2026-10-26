"""Traditional leaf/spine vs Pivot (route servers): BGP paths and route updates on leaf1.

No vtysh: leaf1 streams every route its peers send it over BMP to a collector inside this test.
Run:  .venv/bin/pytest
"""
import pytest

import lab
from bmp import BmpCollector

N = 10_000                                           # GPU routes leaf2 announces
SPINES, RRS, LEAVES = lab.CONFIG["spines"], lab.CONFIG["route_servers"], lab.CONFIG["leaves"]
SPINE1 = "10.0.0.1"                                  # BMP names peers by BGP router-id
PATHS_PER_ROUTE = {"traditional": SPINES,            # one copy from every spine ...
                   "pivot": RRS}                     # ... vs one from every route server


@pytest.fixture(scope="session")
def bmp():
    """BMP collector on the host. leaf1 connects to it (bmp_leaves in group_vars) and pushes its BGP data."""
    return BmpCollector(lab.CONFIG["bmp_port"])


# params: tests using `design` run once per value. scope="module": setup runs once per value.
# yield: code before it is setup, code after it is cleanup.
@pytest.fixture(scope="module", params=["traditional", "pivot"])
def design(request, bmp):
    mode = request.param
    lab.set_mode(mode)
    peers = [f"10.0.0.{s}" for s in range(1, SPINES + 1)]
    if mode != "traditional":
        peers += [f"10.0.0.{100 + r}" for r in range(1, RRS + 1)]
    lab.wait_until(lambda: all(bmp.peer(p)["up"] for p in peers), timeout=300,
                   what=f"{len(peers)} BGP peers up on leaf1")
    lab.scale("add", N)
    lab.wait_until(lambda: bmp.total_routes() >= N * PATHS_PER_ROUTE[mode], timeout=300,
                   what=f"{N * PATHS_PER_ROUTE[mode]:,} BGP paths on leaf1", status=lambda: f"{bmp.total_routes():,}")
    yield mode
    lab.scale("del", N)


def test_route_count(design, bmp):
    paths = bmp.total_routes()
    print(f"\n{design}: leaf1 holds {paths:,} BGP paths for {N:,} GPU routes (same routes, more copies)")
    assert paths >= N * PATHS_PER_ROUTE[design]


def test_route_updates_on_spine1_restart(design, bmp):
    before = bmp.peer(SPINE1)
    lab.log.info("restarting BGP on spine1")
    lab.sh("spine1", "vtysh -c 'clear bgp *'")      # spine1 drops and re-opens all its BGP sessions
    lab.wait_until(lambda: bmp.peer(SPINE1)["downs"] > before["downs"], timeout=30, interval=0.2,
                   what="spine1 session down")
    lab.wait_until(lambda: bmp.peer(SPINE1)["up"] and bmp.peer(SPINE1)["routes"] >= before["routes"],
                   timeout=120, what="spine1 back with all its routes", status=lambda: bmp.peer(SPINE1)["routes"])

    resent = bmp.peer(SPINE1)["announced"] - before["announced"]
    print(f"\n{design}: spine1 re-sent {resent:,} routes to leaf1")
    if design == "pivot":
        assert resent <= SPINES + LEAVES             # loopbacks only
    else:
        assert resent >= N                           # every GPU route
