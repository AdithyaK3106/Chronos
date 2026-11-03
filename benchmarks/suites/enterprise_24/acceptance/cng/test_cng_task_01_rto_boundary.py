"""cng_task_01 acceptance: single-digit RTO codes followed by a category letter.

Delhi plates use a 1-digit RTO code followed by a category letter (C, S, T, ...), e.g.
"DL 1S AB 1234". The OCR correction must not turn that letter into a digit (S->5, T->1),
which silently produces a different, valid-looking plate (DL15AB1234).
"""

import pytest

from app.domain.registration_normalizer import IndianRegistrationNormalizer


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("DL 1S AB 1234", "DL01SAB1234"),
        ("DL 1T AB 1234", "DL01TAB1234"),
        ("DL 1C AB 1234", "DL01CAB1234"),
    ],
)
def test_single_digit_rto_keeps_category_letter(raw: str, expected: str) -> None:
    result = IndianRegistrationNormalizer.normalize(raw)
    assert result.is_valid
    assert result.normalized_plate == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("MH 12 AB 1234", "MH12AB1234"),
        ("KA 5 M 1234", "KA05M1234"),
        ("22 BH 1234 AB", "22BH1234AB"),
    ],
)
def test_existing_formats_unchanged(raw: str, expected: str) -> None:
    result = IndianRegistrationNormalizer.normalize(raw)
    assert result.is_valid
    assert result.normalized_plate == expected
