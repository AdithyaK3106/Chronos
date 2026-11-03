"""Deprecated alias package: use `chronos.agent` (and `chronos.governance.firewall`,
`chronos.context.session`). Kept so existing imports keep working; it will be removed before the
package is published under a generic top-level name.
"""

from __future__ import annotations

import warnings as _warnings
from typing import Any

_warnings.warn(
    "The top-level `runtime` package is deprecated; import from `chronos.agent` instead.",
    DeprecationWarning,
    stacklevel=2,
)


def __getattr__(name: str) -> Any:
    import chronos.agent as _agent

    return getattr(_agent, name)
