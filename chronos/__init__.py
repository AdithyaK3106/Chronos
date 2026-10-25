"""Chronos: Bi-Temporal Repository Intelligence & Autonomous Multi-Agent Governance Engine.

A framework- and agent-agnostic platform providing:
1. Zero-path AST context resolution and token-budgeted envelopes (< 15ms).
2. Sub-file bi-temporal AST concurrency write-leasing for multi-agent fleets.
3. Deterministic pre-tool policy firewalls and cryptographic audit logs.
4. Universal FastMCP server for IDEs, Cursor, Claude Code, Windsurf, and custom agents.
"""

__version__ = "0.2.0"

from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.context.graph import GraphStore, SQLiteGraphStore, create_graph_store
from chronos.context.indexer import index_repository, index_file, extract_file_ast
from chronos.context.linker import CrossFileLinker
from chronos.context.budgeter import assemble_context, estimate_tokens
from chronos.context.resolver import (
    DeterministicContextResolver,
    ResolutionResult,
    HarvestedFeatures,
    CandidateEvidence,
)
from chronos.coordination.ledger import (
    acquire_lock,
    release_lock,
    check_conflicts,
    list_active_locks,
)
from chronos.security.gates import (
    check_gate_approval,
    grant_gate_approval,
    revoke_gate_approval,
    DEFAULT_PROTECTED_PATTERNS,
)
from chronos.security.permissions import (
    check_action_allowed,
    set_permissions,
    get_permissions,
)

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
]
