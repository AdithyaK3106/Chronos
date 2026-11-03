"""cooling_task_01: a read in the same clock tick must not drop the bytes of that interval."""
import sys
from collections import namedtuple
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "src"))
import telemetry_logger as tl  # noqa: E402

Disk = namedtuple("Disk", "read_bytes write_bytes")
Net = namedtuple("Net", "bytes_sent bytes_recv")


def _drive(monkeypatch, collect, counters_attr, make, samples):
    """samples: list of (monotonic_time, total_bytes); returns the rates."""
    it = iter(samples)
    state = {}

    def fake_counters():
        return make(state["bytes"])

    monkeypatch.setattr(tl.psutil, counters_attr, fake_counters)
    rates, prev = [], {}
    for t, total in it:
        state["bytes"] = total
        monkeypatch.setattr(tl.time, "monotonic", lambda t=t: t)
        rate, prev = collect(prev)
        rates.append(rate)
    return rates


@pytest.mark.parametrize("collect, attr, make", [
    (tl.collect_disk_io, "disk_io_counters", lambda b: Disk(b, 0)),
    (tl.collect_network_io, "net_io_counters", lambda b: Net(b, 0)),
])
def test_same_tick_read_keeps_the_interval(monkeypatch, collect, attr, make):
    # t=100 baseline; t=100 again (same tick) after 1000 bytes; t=101 after 3000 bytes in total.
    rates = _drive(monkeypatch, collect, attr, make, [(100.0, 0), (100.0, 1000), (101.0, 3000)])
    assert rates[1] >= 0.0
    assert rates[2] == pytest.approx(3000.0), rates  # the full second, not just the last 2000 bytes


@pytest.mark.parametrize("collect, attr, make", [
    (tl.collect_disk_io, "disk_io_counters", lambda b: Disk(b, 0)),
    (tl.collect_network_io, "net_io_counters", lambda b: Net(b, 0)),
])
def test_normal_rates_are_unchanged(monkeypatch, collect, attr, make):
    rates = _drive(monkeypatch, collect, attr, make, [(10.0, 0), (12.0, 4000), (13.0, 4500)])
    assert rates == [0.0, pytest.approx(2000.0), pytest.approx(500.0)]
