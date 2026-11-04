"""Chronos security, identity, permissions, and gate validation.

Attributes load lazily (PEP 562) so the Claude Code hook, which imports chronos.security.sanitizer
on every call, does not pay for the whole package.
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    "create_agent": ".identity",
    "authenticate_key": ".identity",
    "get_agent": ".identity",
    "hash_key": ".identity",
    "generate_key": ".identity",
    "set_permissions": ".permissions",
    "get_permissions": ".permissions",
    "check_action_allowed": ".permissions",
    "WRITE_TOOLS": ".permissions",
    "sanitize_and_resolve_path": ".sanitizer",
    "validate_path_access": ".sanitizer",
    "redact_secrets": ".sanitizer",
    "check_gate_approval": ".gates",
    "load_protected_modules": ".gates",
    "grant_gate_approval": ".gates",
    "revoke_gate_approval": ".gates",
    "is_gate_approved": ".gates",
    "DEFAULT_PROTECTED_PATTERNS": ".gates",
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
