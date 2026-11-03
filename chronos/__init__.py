"""Chronos: Bi-Temporal Repository Intelligence & Autonomous Multi-Agent Governance Engine.

A framework- and agent-agnostic platform providing:
1. Zero-path AST context resolution and token-budgeted envelopes (< 15ms).
2. Sub-file bi-temporal AST concurrency write-leasing for multi-agent fleets.
3. Deterministic pre-tool policy firewalls and cryptographic audit logs.
4. Universal FastMCP server for IDEs, Cursor, Claude Code, Windsurf, and custom agents.
"""

__version__ = "0.3.0"

# Public API, resolved lazily (PEP 562). Every Claude Code hook call is a new process, and
# importing the indexer, tree-sitter grammars and memory up front cost ~250 ms per call.
import importlib as _importlib
from typing import Any as _Any

_LAZY_EXPORTS: dict[str, str] = {
    "ChronosConfig": "chronos.core.config",
    "get_connection": "chronos.core.db",
    "GraphStore": "chronos.context.graph",
    "SQLiteGraphStore": "chronos.context.graph",
    "create_graph_store": "chronos.context.graph",
    "index_repository": "chronos.context.indexer",
    "index_file": "chronos.context.indexer",
    "extract_file_ast": "chronos.context.indexer",
    "CrossFileLinker": "chronos.context.linker",
    "assemble_context": "chronos.context.budgeter",
    "estimate_tokens": "chronos.context.budgeter",
    "DeterministicContextResolver": "chronos.context.resolver",
    "ResolutionResult": "chronos.context.resolver",
    "HarvestedFeatures": "chronos.context.resolver",
    "CandidateEvidence": "chronos.context.resolver",
    "acquire_lock": "chronos.coordination.ledger",
    "release_lock": "chronos.coordination.ledger",
    "check_conflicts": "chronos.coordination.ledger",
    "list_active_locks": "chronos.coordination.ledger",
    "check_gate_approval": "chronos.security.gates",
    "grant_gate_approval": "chronos.security.gates",
    "revoke_gate_approval": "chronos.security.gates",
    "DEFAULT_PROTECTED_PATTERNS": "chronos.security.gates",
    "check_action_allowed": "chronos.security.permissions",
    "set_permissions": "chronos.security.permissions",
    "get_permissions": "chronos.security.permissions",
    "ChronosMemoryStore": "chronos.memory",
    "MemoryRecord": "chronos.memory",
    "MemoryTier": "chronos.memory",
    "RecallResult": "chronos.memory",
    "on_task_completed": "chronos.memory",
}


def __getattr__(name: str) -> _Any:
    module = _LAZY_EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'chronos' has no attribute {name!r}")
    value = getattr(_importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))

__all__ = [
    "ChronosConfig",
    "get_connection",
    "GraphStore",
    "SQLiteGraphStore",
    "create_graph_store",
    "index_repository",
    "index_file",
    "assemble_context",
    "estimate_tokens",
    "DeterministicContextResolver",
    "ResolutionResult",
    "HarvestedFeatures",
    "CandidateEvidence",
    "acquire_lock",
    "release_lock",
    "check_conflicts",
    "list_active_locks",
    "check_gate_approval",
    "grant_gate_approval",
    "revoke_gate_approval",
    "DEFAULT_PROTECTED_PATTERNS",
    "check_action_allowed",
    "set_permissions",
    "get_permissions",
    "ChronosMemoryStore",
    "MemoryRecord",
    "MemoryTier",
    "RecallResult",
    "on_task_completed",
]
