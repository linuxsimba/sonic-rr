"""Traditional leaf/spine vs HPN Pivot: how much BGP state does leaf1 carry, and how many
UPDATEs does it get when a spine restarts?

Setup per design:  leaf2 announces 10,000 GPU /32s. We look at them from leaf1.
  traditional  leaf1 learns every GPU route from all 8 spines   -> ~8 paths per route
  pivot        leaf1 learns it only from the 2 route servers     -> ~2 paths per route

Run:   .venv/bin/pytest tests/test_bgp_paths.py
"""
import pytest

import lab

N = 10_000                              # GPU routes leaf2 announces
SPINE1 = "Ethernet0"                    # leaf1's BGP neighbor towards spine1 (unnumbered)
PATHS_PER_ROUTE = {"traditional": lab.SPINES, "pivot": 2}


# ---------------------------------------------------------------------------------------------
# FIXTURE: sets the lab up for one design, hands it to the tests, then cleans up.
#   params=[...]     -> pytest runs every test that uses `design` once per value
#   scope="module"   -> the setup runs once per value, not once per test
#   yield            -> code before it is setup, code after it is teardown
# ---------------------------------------------------------------------------------------------
@pytest.fixture(scope="module", params=["traditional", "pivot"])
def design(request):
    mode = request.param
    lab.set_mode(mode, bfd="off")                     # MODE=<mode> ./configure.sh
    lab.wait_converged(mode)
    lab.scale("add", N)                               # leaf2 announces 10k GPU /32s
    lab.wait_until(lambda: lab.bgp_memory()[0] >= N * PATHS_PER_ROUTE[mode],
                   timeout=300, what=f"{N} routes x {PATHS_PER_ROUTE[mode]} paths on leaf1")
    yield mode                                        # tests run here
    lab.scale("del", N)                               # teardown


# ---------------------------------------------------------------------------------------------
# TEST 1: BGP state on leaf1.   `design` and `results` are fixtures: pytest passes them in.
# ---------------------------------------------------------------------------------------------
def test_bgp_paths(design, results):
    paths, mem_bytes = lab.bgp_memory()              # vtysh: show bgp memory
    results.add("leaf1 BGP state (10k GPU routes)", design,
                bgp_paths=paths, bgp_memory_kib=mem_bytes // 1024)

    expected = N * PATHS_PER_ROUTE[design]
    assert paths >= expected, f"expected at least {expected} BGP paths, got {paths}"
    assert paths < expected + N, "more paths than the design should produce"


# ---------------------------------------------------------------------------------------------
# TEST 2: spine1 restarts its BGP. What does leaf1 have to receive again?
# ---------------------------------------------------------------------------------------------
def test_spine1_restart(design, results):
    before = lab.bgp_neighbor(SPINE1)
    all_before = lab.bgp_update_counters("leaf1")

    lab.sh("spine1", "vtysh -c 'clear bgp *'")      # spine1 drops and re-opens all sessions

    # wait for the session to go down, then come back with the same prefixes
    lab.wait_until(lambda: lab.bgp_neighbor(SPINE1)["state"] != "Established",
                   timeout=30, interval=0.2, what="spine1 session reset")
    lab.wait_until(lambda: (n := lab.bgp_neighbor(SPINE1))["state"] == "Established"
                   and n["prefixes"] == before["prefixes"],
                   timeout=120, interval=0.5, what="spine1 session back with all prefixes")

    after = lab.bgp_neighbor(SPINE1)
    total = lab.diff(lab.bgp_update_counters("leaf1"), all_before)
    results.add("spine1 BGP restart, seen by leaf1", design,
                prefixes_from_spine1=after["prefixes"],
                updates_from_spine1=after["updates_rcvd"] - before["updates_rcvd"],
                updates_from_all_peers=sum(r for r, _ in total.values()))

    if design == "pivot":
        # spines only carry loopbacks in pivot: spine1 re-sends just a couple of routes
        assert after["prefixes"] < 10
    else:
        # traditional: spine1 re-sends every GPU route
        assert after["prefixes"] >= N
