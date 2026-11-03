"""mediassist_task_04: bilingual warning banner for high-urgency triage results."""
import re

import pytest

from src.conversational.response_formatter import ResponseFormatter

DEVANAGARI = re.compile(r"[ऀ-ॿ]")
PREDS = [{"disease": "Dengue", "probability": 0.62}, {"disease": "Malaria", "probability": 0.21}]
# format_predictions() output at the baseline commit, which must not change without the new arguments.
BASELINE = (
    "### Possible Conditions:\n1. Dengue — 62.0%\n2. Malaria — 21.0%\n\n"
    "### Suggested Action:\nPlease consult a healthcare professional for an accurate diagnosis."
)


@pytest.fixture
def fmt():
    return ResponseFormatter()


def test_high_urgency_banner_in_english_and_hindi(fmt):
    en = fmt.urgency_banner("High", language="en")
    hi = fmt.urgency_banner("High", language="hi")
    assert en.startswith("⚠️") and hi.startswith("⚠️")
    assert not DEVANAGARI.search(en) and re.search(r"[A-Za-z]{4}", en)
    assert len(DEVANAGARI.findall(hi)) >= 10
    assert fmt.urgency_banner("High") == en  # English is the default


@pytest.mark.parametrize("level", ["Moderate", "Low"])
def test_no_banner_below_high(fmt, level):
    assert fmt.urgency_banner(level, language="en") == "" and fmt.urgency_banner(level, language="hi") == ""


def test_format_predictions_prepends_the_banner_only_when_high(fmt):
    out = fmt.format_predictions(PREDS, urgency_level="High", language="hi")
    assert out.startswith(fmt.urgency_banner("High", language="hi"))
    assert out.endswith(BASELINE)
    assert fmt.format_predictions(PREDS, urgency_level="Low") == BASELINE
    assert fmt.format_predictions(PREDS) == BASELINE
