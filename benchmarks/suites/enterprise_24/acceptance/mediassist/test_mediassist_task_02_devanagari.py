"""mediassist_task_02: Hindi (Devanagari) symptoms keep their letters and vowel signs; punctuation is dropped."""
import unicodedata

import pytest

from src.preprocessing.symptom_processor import normalize_symptom


def nfc(s):
    return unicodedata.normalize("NFC", s)


@pytest.mark.parametrize("raw, expected", [
    ("तेज़ बुखार", "तेज़_बुखार"),           # high fever: vowel signs and nukta survive
    ("सिर दर्द।", "सिर_दर्द"),               # headache, ending with a danda
    ("खांसी, बुखार", "खांसी_बुखार"),         # comma between symptoms
    ("सांस लेने में तकलीफ़॥", "सांस_लेने_में_तकलीफ़"),  # double danda
])
def test_devanagari_symptoms_are_normalized_not_corrupted(raw, expected):
    assert nfc(normalize_symptom(raw)) == nfc(expected)


@pytest.mark.parametrize("raw, expected", [
    ("High Fever", "high_fever"),
    ("chest-pain!", "chest_pain"),
    ("  Runny   Nose ", "runny_nose"),
])
def test_english_behaviour_is_unchanged(raw, expected):
    assert normalize_symptom(raw) == expected
