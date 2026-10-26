import json
import os
import subprocess
import time
from collections import defaultdict

import pytest

import lab
from watchers import GnmiRouteWatcher, KernelRouteWatcher


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: long-running lab test")


class Results:
    """Collects metric samples per (test, mode) and prints a comparison table at the end."""

    def __init__(self):
        self.rows = defaultdict(lambda: defaultdict(list))   # (section, metric) -> mode -> [values]

    def add(self, section, mode, **metrics):
        for k, v in metrics.items():
            self.rows[(section, k)][mode].append(v)

    @staticmethod
    def fmt(vals):
        nums = [v for v in vals if isinstance(v, (int, float))]
        if len(nums) == len(vals) and nums:
            avg = sum(nums) / len(nums)
            s = f"{avg:,.0f}" if avg >= 100 else f"{avg:,.2f}"
            return s + (f" ({min(nums):,.4g}-{max(nums):,.4g})" if len(nums) > 1 else "")
        return ", ".join(str(v) for v in vals)

    def report(self):
        order = ["traditional", "pivot"]
        seen = {m for r in self.rows.values() for m in r}
        modes = [m for m in order if m in seen] + sorted(seen - set(order))
        w = 36
        lines = [f"{'metric':<{w + 14}}" + "".join(f"{m:>26}" for m in modes)]
        section = None
        for (sec, metric), per in self.rows.items():
            if sec != section:
                lines.append(f"\n[{sec}]")
                section = sec
            lines.append(f"  {metric:<{w + 12}}" + "".join(f"{self.fmt(per.get(m, ['-'])):>26}" for m in modes))
        return "\n".join(lines)


@pytest.fixture(scope="session")
def results():
    r = Results()
    yield r
    print("\n\n================ RESULTS ================\n" + r.report())
    os.makedirs(os.path.join(lab.REPO, "results"), exist_ok=True)
    path = os.path.join(lab.REPO, "results", time.strftime("bgp-%Y%m%d-%H%M%S.json"))
    with open(path, "w") as f:
        json.dump({f"{s}|{m}": dict(v) for (s, m), v in r.rows.items()}, f, indent=2)
    print(f"\nresults written to {path}")


@pytest.fixture(scope="session")
def gnmi():
    """gNMI STREAM subscription to leaf1's APPL_DB ROUTE_TABLE (starts the sidecar if needed)."""
    running = subprocess.run(["docker", "ps", "-q", "-f", "name=^gnmi-leaf1$"],
                             capture_output=True, text=True).stdout.strip()
    if not running:
        subprocess.run(["gnmi/setup.sh", "leaf1"], cwd=lab.REPO, check=True)
        time.sleep(5)
    ip = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                         f"{lab.LAB}-leaf1"], capture_output=True, text=True).stdout.strip()
    return GnmiRouteWatcher(ip)


@pytest.fixture(scope="session")
def kernel():
    w = KernelRouteWatcher("leaf1")
    yield w
    w.close()
