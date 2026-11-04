from datetime import datetime, timezone

import pytest

from shopkit import clock
from shopkit.coupons import discount
from shopkit.error_codes import CODES
from shopkit.errors import ShopError


def teardown_function():
    clock.set_now(None)


def test_expired_coupon_is_rejected():
    clock.set_now(datetime(2026, 6, 15, tzinfo=timezone.utc))
    with pytest.raises(ShopError) as e:
        discount("SPRING20", 1000)
    assert e.value.code in CODES


def test_coupon_valid_before_expiry():
    clock.set_now(datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert discount("SPRING20", 1000) == 200


def test_other_coupon_unaffected():
    clock.set_now(datetime(2026, 6, 15, tzinfo=timezone.utc))
    assert discount("WELCOME10", 1000) == 100
