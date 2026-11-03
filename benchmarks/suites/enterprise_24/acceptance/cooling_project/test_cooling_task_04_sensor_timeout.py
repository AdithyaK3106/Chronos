"""cooling_task_04: sensor communication timeout -> FAILSAFE mode + one failsafe broadcast packet."""
import json

import pytest

import src.thermal_mode_controller as tmc


class FakeHardware:
    def __init__(self, *a, **k):
        self.actual_hardware_mode = "BALANCED"
        self.calls = []

    def set_mode(self, mode, reason="", severity=""):
        self.calls.append((mode, reason, severity))
        return True

    def reconcile(self):
        return True


@pytest.fixture
def ctl(monkeypatch):
    monkeypatch.setattr(tmc, "HardwareController", FakeHardware)
    c = tmc.ThermalModeController()
    c.sent = []
    c.broadcast = lambda packet: c.sent.append(packet)
    return c


def test_no_packet_while_sensor_is_fresh(ctl):
    assert ctl.check_sensor_timeout(last_sample_time=100.0, now=103.0) is None
    assert ctl.sent == [] and ctl.active_mode != tmc.ThermalMode.FAILSAFE


def test_timeout_switches_to_failsafe_and_broadcasts(ctl):
    packet = ctl.check_sensor_timeout(last_sample_time=100.0, now=107.5)
    assert ctl.active_mode == tmc.ThermalMode.FAILSAFE
    assert ctl.hardware_controller.calls[-1][0] == "FAILSAFE"
    assert ctl.sent == [packet]
    assert packet["type"] == "THERMAL_FAILSAFE" and packet["reason"] == "SENSOR_TIMEOUT"
    assert packet["mode"] == "FAILSAFE" and packet["silence_s"] == pytest.approx(7.5)
    assert packet["timestamp"] == pytest.approx(107.5)
    json.dumps(packet)  # must be JSON-encodable


def test_one_packet_per_timeout_episode(ctl):
    ctl.check_sensor_timeout(last_sample_time=100.0, now=106.0)
    assert ctl.check_sensor_timeout(last_sample_time=100.0, now=109.0) is None   # same silence
    ctl.check_sensor_timeout(last_sample_time=110.0, now=111.0)                  # sensor is back
    assert ctl.check_sensor_timeout(last_sample_time=110.0, now=120.0) is not None  # new episode
    assert len(ctl.sent) == 2


def test_custom_timeout(ctl):
    assert ctl.check_sensor_timeout(last_sample_time=100.0, now=101.5, timeout_s=1.0) is not None


def test_default_broadcast_sends_udp_and_never_raises(monkeypatch):
    monkeypatch.setattr(tmc, "HardwareController", FakeHardware)
    sent = []

    class FakeSocket:
        def __init__(self, *a, **k): self.opts = []
        def setsockopt(self, *a): self.opts.append(a)
        def sendto(self, data, addr): sent.append((json.loads(data.decode("utf-8")), addr, list(self.opts)))
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(tmc.socket, "socket", FakeSocket)
    tmc.ThermalModeController().broadcast({"type": "THERMAL_FAILSAFE"})
    (payload, addr, opts), = sent
    assert payload == {"type": "THERMAL_FAILSAFE"} and addr == ("255.255.255.255", 50505)
    assert any(o[1] == tmc.socket.SO_BROADCAST for o in opts)

    class Broken(FakeSocket):
        def sendto(self, *a): raise OSError("network unreachable")

    monkeypatch.setattr(tmc.socket, "socket", Broken)
    tmc.ThermalModeController().broadcast({"type": "THERMAL_FAILSAFE"})  # logged, not raised
