"""Chronos core modules.

Attributes are loaded lazily (PEP 562): `import chronos.core.repo` is on the path of every Claude
Code hook call, and eagerly importing the pydantic models here added ~150 ms to each one.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "ChronosError": ".exceptions",
    "ChronosSecurityError": ".exceptions",
    "PathTraversalError": ".exceptions",
    "PolicyViolationError": ".exceptions",
    "LockConflictError": ".exceptions",
    "LockOwnershipError": ".exceptions",
    "ASTNode": ".models",
    "ASTEdge": ".models",
    "ImportBinding": ".models",
    "LockInfo": ".models",
    "ProvenanceEvent": ".models",
    "AuditEvent": ".models",
    "PermissionManifest": ".models",
    "ChronosConfig": ".config",
    "get_connection": ".db",
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
