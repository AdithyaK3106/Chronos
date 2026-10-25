"""Chronos security, identity, permissions, and gate validation."""
from .identity import create_agent, authenticate_key, get_agent, hash_key, generate_key
from .permissions import set_permissions, get_permissions, check_action_allowed, WRITE_TOOLS
from .sanitizer import sanitize_and_resolve_path, validate_path_access, redact_secrets
from .gates import check_gate_approval, load_protected_modules

__all__ = [
    "create_agent",
    "authenticate_key",
    "get_agent",
    "hash_key",
    "generate_key",
    "set_permissions",
    "get_permissions",
    "check_action_allowed",
    "WRITE_TOOLS",
    "sanitize_and_resolve_path",
    "validate_path_access",
    "redact_secrets",
    "check_gate_approval",
    "load_protected_modules",
]
