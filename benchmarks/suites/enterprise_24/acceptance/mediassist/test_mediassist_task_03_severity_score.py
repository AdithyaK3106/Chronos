"""mediassist_task_03: weighted severity score from vitals + symptom severity; high scores force High urgency."""
import pytest

from src.safety.decision_layer import ClinicalDecisionLayer, compute_severity_score


@pytest.mark.parametrize("vitals, severity, expected", [
    ({}, "mild", 0.10),
    ({}, "moderate", 0.25),
    ({}, "severe", 0.40),
    ({"temperature_c": 38.6}, "mild", 0.25),
    ({"temperature_c": 39.8, "heart_rate": 125}, "moderate", 0.85),
    ({"spo2": 93}, "mild", 0.30),
    ({"spo2": 89, "systolic_bp": 85}, "severe", 1.0),   # clipped to 1.0
    ({"heart_rate": 105, "spo2": 97}, "mild", 0.25),
])
def test_score_formula(vitals, severity, expected):
    assert compute_severity_score(vitals, severity) == pytest.approx(expected, abs=1e-6)


def test_unknown_severity_counts_as_moderate_and_missing_vitals_are_ignored():
    assert compute_severity_score({"heart_rate": None}, "unknown") == pytest.approx(0.25)


@pytest.fixture(scope="module")
def layer():
    return ClinicalDecisionLayer()


def test_dangerous_vitals_force_high_urgency(layer):
    preds = [{"disease": "Common Cold", "probability": 0.80}]
    timeline = {"duration_days": 1, "severity": "mild", "progression": "stable"}
    res = layer.evaluate(preds, timeline, vitals={"spo2": 88, "heart_rate": 125})
    assert res["urgency_level"] == "High" and res["allow_home_care"] is False
    assert res["severity_score"] == pytest.approx(0.8)


def test_without_vitals_urgency_is_unchanged(layer):
    preds = [{"disease": "Common Cold", "probability": 0.80}]
    timeline = {"duration_days": 1, "severity": "mild", "progression": "stable"}
    res = layer.evaluate(preds, timeline)
    assert res["urgency_level"] == "Low" and res["allow_home_care"] is True
