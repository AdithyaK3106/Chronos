# Chronos Migration Manifest

## Source & Destination Mapping

| Component | Source (`Chronos-old`) | Destination (`chronos/`) | Decision | Reason & Architectural Enhancements |
|---|---|---|---|---|
| **Sub-File Node Locking** | `chronos/ledger.py` | `chronos/coordination/ledger.py` | **Migrated** | Preserved atomic SQLite `IMMEDIATE` transaction locking, 300s lease TTL, mutual exclusion, and crash recovery. Adapted to unified SQLite WAL connection manager. |
| **Audit Logging** | `chronos/audit.py` | `chronos/audit/logger.py` | **Migrated** | Append-only SHA-256 hash chaining retained. Enhanced with strict regex secret scrubbing before serialization. |
| **Path Permissions** | `chronos/permissions.py` | `chronos/security/permissions.py` | **Migrated** | Least-privilege path policies retained. Enhanced with canonical `resolve()` path sanitization to defeat `..` traversal and symlink escapes. |
| **Agent Identity** | `chronos/identity.py` | `chronos/security/identity.py` | **Migrated** | Cryptographic API key generation and salted SHA-256 hash indexing. |
| **Approval Gates** | `chronos/gates.py` | `chronos/security/gates.py` | **Migrated** | Human-in-the-loop protected path gates (`.chronos/protected.yml`). Fails closed on corruption. |
| **Bi-Temporal Graph** | `chronos/query.py`, `chronos/sync.py` | `chronos/context/graph.py` | **Modernized** | Discarded Graphiti LLM wrapper. Implemented dual-engine `GraphStore` (SQLite WAL default + Kùzu portability gate). |
| **AST Indexer** | `chronos/indexer.py`, `build_cbm.py` | `chronos/context/indexer.py` | **Replaced** | Replaced vendored C `codebase-memory-mcp` (which required a 3-minute C toolchain compile) with pure Python AST & Tree-sitter indexer (`tree-sitter>=0.25`). |
| **CI Merge Gates** | `chronos/enforcer.py`, `rule_store.py` | `chronos/verification/enforcer.py` | **Migrated** | Self-contained rule lifecycle (`proposed`, `warn`, `blocking`) and AST scanning. |
| **OpenHands Runtime** | *New* | `runtime/` | **New** | Clean integration harness for OpenHands SDK `Agent`, `Conversation`, `PreToolUse` hook interceptor, and `ChronosContextCondenser`. |
| **Dashboards & Presentations** | `dashboard.html`, `graph.html`, `presentation.html` | *None* | **Discarded** | Outdated demo and prototype presentations omitted from clean production repository. |
| **External Submodules** | `vendor/codebase-memory-mcp`, `pilot_target` | *None* | **Discarded** | Brittle submodules eliminated in favor of standard pre-compiled Python wheels. |
