from datetime import datetime, timezone

from shopkit import clock
from shopkit.errors import ShopError

# code -> (percent off, created at, expires at or None)
COUPONS = {
    "WELCOME10": (10, datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 12, 31, tzinfo=timezone.utc)),
    "SPRING20": (20, datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 5, 31, tzinfo=timezone.utc)),
}


def discount(code: str, subtotal_cents: int) -> int:
    """Discount in cents for a coupon code."""
    if code not in COUPONS:
        raise ShopError("COUPON_UNKNOWN")
    percent, created, _expires = COUPONS[code]
    if clock.now() < created:
        raise ShopError("COUPON_UNKNOWN")
    return int(subtotal_cents * percent / 100 + 0.5)
