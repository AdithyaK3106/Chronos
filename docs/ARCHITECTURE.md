# Chronos Architecture & Dependency Map

## 1. System Overview

Chronos is a bi-temporal code knowledge graph and deterministic multi-agent AI governance engine built on the OpenHands Software Agent SDK (`openhands-sdk==1.58.2`).

```
┌────────────────────────────────────────────────────────────────────────┐
│                        AGENT RUNTIME HARNESS                           │
│     (OpenHands SDK Agent · Conversation · Condenser · TestLLM)         │
└──────────────────┬───────────────────────────────┬─────────────────────┘
                   │                               │
        ┌──────────▼──────────┐         ┌──────────▼──────────┐
        │ PreToolUse Security │         │   Chronos Context   │
        │     Interceptor     │         │      Condenser      │
        │   (runtime/hooks)   │         │ (runtime/condenser) │
        └──────────┬──────────┘         └──────────┬──────────┘
                   │                               │
┌──────────────────▼───────────────────────────────▼─────────────────────┐
│                            CHRONOS CORE                                │
│                                                                        │
│   ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐    │
│   │   Coordination    │ │     Security      │ │  Bi-Temporal AST  │    │
│   │  AST Node Locking │ │ Path Permissions  │ │   Knowledge Graph │    │
│   │    & Lease TTL    │ │   Approval Gates  │ │ (SQLite WAL+Kùzu) │    │
│   └─────────┬─────────┘ └─────────┬─────────┘ └─────────┬─────────┘    │
│             │                     │                     │              │
│             └─────────────────────┼─────────────────────┘              │
│                                   │                                    │
│                         ┌─────────▼─────────┐                          │
│                         │ Tamper-Evident    │                          │
│                         │  Audit Log (SHA)  │                          │
│                         └───────────────────┘                          │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Package Decomposition

| Package | Purpose | Key Modules |
|---|---|---|
| `runtime/` | OpenHands SDK integration layer | `hooks.py`, `condenser.py`, `openhands_adapter.py` |
| `chronos/context/` | Codebase AST indexing, bi-temporal graph, and token budgeting | `indexer.py`, `graph.py`, `budgeter.py`, `kuzu_backend.py` |
| `chronos/coordination/` | Sub-file AST node locking, lease TTL, and mutual exclusion | `ledger.py` |
| `chronos/security/` | Least-privilege permissions, symlink defense, identity, and gates | `permissions.py`, `sanitizer.py`, `identity.py`, `gates.py` |
| `chronos/verification/` | Deterministic AST merge gates and rule lifecycle | `rule_store.py`, `enforcer.py` |
| `chronos/audit/` | Append-only SHA-256 hash-chained event ledger | `logger.py` |
| `chronos/telemetry/` | Token telemetry, cost metrics, and provenance stamps | `metrics.py` |
| `chronos/server/` | FastMCP 3.x unified stdio server for external coding agents | `mcp_server.py` |
| `chronos/cli/` | Command line utilities (`init`, `doctor`, `sync`, `audit`) | `main.py` |

---

## 3. Dependency Map

### Core Production Dependencies (Cross-Platform Wheels)
- `openhands-sdk>=1.58.2`: Agent runtime harness, conversation state machine, hook manager.
- `openhands-tools>=1.58.2`: Builtin agent tool implementations (`file_editor`, `terminal`).
- `tree-sitter>=0.25.0`: Universal parser engine (precompiled wheels on Linux, macOS Apple Silicon/Intel, Windows).
- `tree-sitter-bash>=0.25.0`: Bash grammar parser.
- `pydantic>=2.10.0`: Strictly typed data models with serialization and schema validation.
- `fastmcp>=3.2.0,<4`: MCP protocol transport.
- `pyyaml>=6.0.0`: Permission manifests and gate configuration parsing.

### Optional Acceleration
- `kuzu>=0.11.0`: Embedded property graph engine (used when present via portability gate).
- `pytest>=8.0`: Test runner extra.
