"""pytest adapter for the assert-based suites.

The suites are standalone scripts with their own __main__ (run them directly for
readable per-check output). Without this shim `pytest tests/ -q` collected
nothing and reported success -- a green result that proved nothing. Each suite
runs here as a subprocess so a crash in one cannot take down the others.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# test_unification re-runs the other four internally as its own regression
# check, so they execute twice under pytest. Kept deliberately: each suite gets
# its own pass/fail line here, which is what makes a failure readable.
SUITES = ["test_chronos", "test_wedge2", "test_wedge3", "test_wedge4",
          "test_unification"]


@pytest.mark.parametrize("suite", SUITES)
def test_suite(suite):
    r = subprocess.run([sys.executable, str(ROOT / "tests" / f"{suite}.py")],
                       capture_output=True, text=True, timeout=1800, cwd=ROOT)
    assert r.returncode == 0 and "ALL PASS" in r.stdout, (
        f"{suite} failed (exit {r.returncode})\n"
        f"--- stdout ---\n{r.stdout[-3000:]}\n--- stderr ---\n{r.stderr[-2000:]}")
