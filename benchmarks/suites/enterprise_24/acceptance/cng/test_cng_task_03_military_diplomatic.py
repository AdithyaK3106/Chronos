"""cng_task_03: Indian military (arrow) and diplomatic (CD/CC/UN) plates are recognised and validated."""
import pytest

from app.domain.registration_normalizer import IndianRegistrationNormalizer as N


@pytest.mark.parametrize("raw, normalized, formatted", [
    ("↑ 15 B 123456 K", "15B123456K", "15 B 123456 K"),
    ("↑15B123456K", "15B123456K", "15 B 123456 K"),
    ("09 D 654321 X", "09D654321X", "09 D 654321 X"),
])
def test_military_plates(raw, normalized, formatted):
    r = N.normalize(raw)
    assert (r.is_valid, r.normalized_plate, r.formatted_plate) == (True, normalized, formatted)


@pytest.mark.parametrize("raw, normalized, formatted", [
    ("77 CD 12", "77CD12", "77 CD 12"),
    ("125 CC 3", "125CC3", "125 CC 3"),
    ("1 UN 4021", "1UN4021", "1 UN 4021"),
])
def test_diplomatic_plates(raw, normalized, formatted):
    r = N.normalize(raw)
    assert (r.is_valid, r.normalized_plate, r.formatted_plate) == (True, normalized, formatted)


@pytest.mark.parametrize("raw", ["15 B 12345 K", "77 XY 12", "1234 CD 1", "77 CD 12345"])
def test_lookalikes_stay_invalid(raw):
    assert N.normalize(raw).is_valid is False


@pytest.mark.parametrize("raw, normalized", [
    ("MH 12 AB 1234", "MH12AB1234"),
    ("22 BH 4567 AA", "22BH4567AA"),
    ("KA 5 MN 2020", "KA05MN2020"),
])
def test_existing_formats_unchanged(raw, normalized):
    r = N.normalize(raw)
    assert r.is_valid and r.normalized_plate == normalized
