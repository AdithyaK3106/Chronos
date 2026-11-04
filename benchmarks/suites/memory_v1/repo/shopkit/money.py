"""Money helpers. Every amount in shopkit is an integer number of cents."""


def format_money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def percent_of(cents: int, percent: float) -> int:
    """`percent` of an amount, rounded half up to the cent."""
    return int(cents * percent / 100 + 0.5)
