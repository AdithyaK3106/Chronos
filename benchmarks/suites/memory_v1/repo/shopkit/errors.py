from shopkit.error_codes import CODES


class ShopError(Exception):
    """The only exception shopkit raises to callers; `code` must be a key of error_codes.CODES."""

    def __init__(self, code: str, message: str | None = None):
        if code not in CODES:
            raise KeyError(f"unregistered error code {code}")
        self.code = code
        super().__init__(message or CODES[code])
