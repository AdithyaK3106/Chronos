"""mediassist_task_01: negated chest-pain mentions must not trigger the emergency veto."""
import pytest

from src.safety.emergency_rules import EmergencyRuleEngine


@pytest.fixture(scope="module")
def engine():
    return EmergencyRuleEngine()


@pytest.mark.parametrize("text", [
    "No chest pain, just a sore throat",
    "I deny chest pain and shortness of breath is absent too",
    "Coughing a bit but without any chest pain",
    "I don't have chest pain, my knee hurts after running",
    "Patient denies chest pain.",
])
def test_negated_chest_pain_is_not_an_emergency(engine, text):
    res = engine.check_for_emergency(text)
    assert "Cardiac Emergency" not in res.get("categories", []), res


@pytest.mark.parametrize("text", [
    "I have chest pain",
    "Sudden chest pain spreading to my left arm",
    "No fever but I have chest pain since morning",
    "crushing chest pain",
])
def test_real_chest_pain_still_triggers(engine, text):
    res = engine.check_for_emergency(text)
    assert res["is_emergency"] and "Cardiac Emergency" in res["categories"], res
