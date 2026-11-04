"""The current time. Tests freeze it with set_now()."""
from datetime import datetime, timezone

_frozen = None


def now() -> datetime:
    return _frozen or datetime.now(timezone.utc)


def set_now(value):
    global _frozen
    _frozen = value
