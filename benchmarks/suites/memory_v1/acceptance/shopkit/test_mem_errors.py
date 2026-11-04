import pytest

from shopkit.cart import User
from shopkit.checkout import place_order
from shopkit.error_codes import CODES
from shopkit.errors import ShopError


def test_empty_cart_fails_with_a_registered_error():
    with pytest.raises(ShopError) as e:
        place_order([], User("u1"))
    assert e.value.code in CODES and e.value.code not in ("COUPON_UNKNOWN", "OUT_OF_STOCK")
