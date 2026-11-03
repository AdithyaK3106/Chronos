"""cooling_task_03: a hysteresis band suppresses small fan-speed steps (rapid oscillation)."""
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd() / "src"))
import cooling_policy  # noqa: E402
from cooling_policy import apply_fan_hysteresis  # noqa: E402


@pytest.mark.parametrize("current, target, band, expected", [
    (50.0, 53.0, 5.0, 50.0),    # small step up: suppressed
    (50.0, 46.0, 5.0, 50.0),    # small step down: suppressed
    (50.0, 55.0, 5.0, 55.0),    # at the band edge: applied
    (50.0, 65.0, 5.0, 65.0),    # large step: applied
    (30.0, 20.0, 5.0, 20.0),
    (97.0, 100.0, 5.0, 100.0),  # failsafe target always passes immediately
    (50.0, 52.0, 0.0, 52.0),    # band 0 disables hysteresis
])
def test_hysteresis_band(current, target, band, expected):
    assert apply_fan_hysteresis(current, target, band) == pytest.approx(expected)


def test_default_band_is_five_points():
    assert apply_fan_hysteresis(40.0, 44.9) == pytest.approx(40.0)
    assert apply_fan_hysteresis(40.0, 45.0) == pytest.approx(45.0)


def test_oscillating_targets_do_not_move_the_fan():
    speed = 50.0
    for target in [52.0, 48.5, 53.0, 47.0, 51.0, 49.0]:
        speed = apply_fan_hysteresis(speed, target)
    assert speed == pytest.approx(50.0)


def test_engine_uses_it():
    engine = cooling_policy.CoolingPolicyEngine()
    assert engine.fan_hysteresis_band == pytest.approx(5.0)
    assert "apply_fan_hysteresis" in inspect.getsource(cooling_policy.CoolingPolicyEngine.update)
