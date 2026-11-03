"""mediassist_task_05: the bilingual symptom synonym map lives in an external JSON file; behaviour unchanged."""
import json
from pathlib import Path

import pytest

ROOT = Path.cwd()
JSON_PATH = ROOT / "src" / "conversational" / "symptom_synonyms.json"
MODULE_PATH = ROOT / "src" / "conversational" / "timeline_extractor.py"

EXPECTED_KEYS = ["fever", "headache", "cough", "body pain", "sore throat", "vomiting", "diarrhea",
                 "stomach pain", "rash", "shortness of breath"]

# Outputs of the heuristic extractor at the baseline commit: a refactor must not change them.
BASELINE = {
    "I have had fever and headache for 3 days": ["fever", "headache"],
    "mujhe bukhar aur khansi hai since yesterday": ["fever", "cough"],
    "मुझे सिर दर्द और उल्टी हो रही है": ["headache"],
    "severe stomach pain and loose motion, getting worse": ["diarrhea", "stomach pain"],
    "skin rash with itching": ["rash"],
    "I feel fine today": ["unspecified malaise"],
}


def test_synonyms_live_in_the_json_file():
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    assert list(data) == EXPECTED_KEYS
    assert all(isinstance(v, list) and v and all(isinstance(x, str) for x in v) for v in data.values())
    assert "sar dard" in data["headache"] and "बुखार" in data["fever"]


def test_module_no_longer_hardcodes_them_but_still_exposes_the_map():
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "sans lene me takleef" not in source and "gale me dard" not in source
    from src.conversational.timeline_extractor import COMMON_SYMPTOM_MAP

    assert COMMON_SYMPTOM_MAP == json.loads(JSON_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("text, symptoms", list(BASELINE.items()))
def test_extraction_behaviour_is_unchanged(text, symptoms):
    from src.conversational.timeline_extractor import SymptomTimelineExtractor

    assert SymptomTimelineExtractor().extract(text)["symptoms"] == symptoms
