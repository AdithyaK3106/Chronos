# ⏳ Chronos

**Bi-Temporal Repository Intelligence & Deterministic Multi-Agent Governance Engine**
*Built on the OpenHands Software Agent SDK (v1.58.2)*

---

## 💡 Overview

Chronos enables autonomous coding agents to work concurrently on production repositories without context blindness, merge clobbers, zombie API calls, or security bypasses:

1. **Bi-Temporal AST Knowledge Graph (`chronos/context/`):** Answers *"what called this symbol as-of commit/timestamp X?"* with sub-millisecond SQLite WAL queries (0.029 ms/lookup) and optional Kùzu property graph acceleration.
2. **Deterministic Lifecycle Interception (`runtime/hooks.py`):** Hooks natively into OpenHands SDK `PreToolUse` events to block unauthorized file edits, path traversals, symlink escapes, and stale locks before any tool executes.
3. **Sub-File AST Node Locking (`chronos/coordination/`):** Prevents multi-agent collisions with atomic AST function/class leases, automatic TTL expiration, and crash recovery.
4. **Tamper-Evident Provenance & Audit (`chronos/audit/`):** Append-only SHA-256 hash-chained event ledger with strict secret redaction.
5. **Context Optimization & Budgeting (`runtime/condenser.py`):** OpenHands context condenser assembling relevance-ranked AST context under configurable token budgets.

## 🚀 Quick Start

### Installation
```bash
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[test,kuzu]"
```

### Running Tests
```bash
pytest tests/ -v
```
