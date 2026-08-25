# Chronos — Bi-Temporal AST Knowledge Graph & AI Governance

Gives AI coding agents a historical memory of what your codebase **used to** look like, prevents multi-agent collisions, and enforces CI merge gates so agents stop confidently reintroducing patterns you deleted last month.

---

```
$ chronos --repo . sync
synced myrepo @ 2026-08-12T17:34:26+00:00 in 0.8s: nodes=46 added=4 invalidated=6 unchanged=74
```

```
callers of health()               → before refactor: [main, do_doctor, do_health]
                                  → now:             NONE (superseded)
callers of index_health_report()  → before refactor: NONE (did not exist)
                                  → now:             [do_health, do_doctor]
```

---

## ⚡ Quick Start

### 1. Install

```bash
git submodule update --init --depth 1   # vendored indexer source (MIT)
pip install -e .
python -m chronos.build_cbm             # build the local indexer (~5 min, requires C toolchain)
```

> **Requirements:** Python 3.10+, C toolchain (GCC/Clang or MSYS2 on Windows: `pacman -S mingw-w64-ucrt-x86_64-gcc make`).  
> Run `chronos doctor` anytime to verify toolchain, DB status, and schema health.

### 2. Initialize a Repository

In any git repository, run:

```bash
python -m chronos init --repo .
```

This creates `.chronos/`, generates `config.json`, sets up git hooks (`pre-commit` → enforce, `post-merge` → index), and registers the MCP block in Claude Desktop and Cursor.

### 3. Register with Claude Code / Cursor

Add Chronos as a single stdio MCP server:

```json
{
  "mcpServers": {
    "chronos": {
      "command": "chronos-mcp",
      "env": {
        "CHRONOS_GROUP_ID": "myrepo",
        "CHRONOS_AUTH": "permissive"
      }
    }
  }
}
```

That's the complete configuration — **26 tools across all 4 wedges and F1–F7 governance controls** with **zero network egress**.

---

## 🏛️ Architecture & Core Wedges

Chronos operates as a single unified MCP server (`chronos-mcp`) backed by an embedded graph store ([Kùzu](https://kuzudb.com/)) and an embedded SQLite database (`chronos.db`):

| Layer | Capability | Description |
|---|---|---|
| **Wedge 1** | **Temporal Graph** | Bi-temporal AST knowledge graph tracking codebase evolution over time. Answers *"what was true as-of timestamp X?"* |
| **Wedge 2** | **Policy Playbook** | Self-healing rule engine that learns from failed code reviews and proposes enforceable AST rules. |
| **Wedge 3** | **Intent Ledger** | Sub-file AST node locking (`function`/`class` level) to prevent multi-agent collisions and race conditions. |
| **Wedge 4** | **CI Enforcement** | Deterministic merge gates running AST-grep and OPA policies against git diffs in under 1 second. |

### Feedback Loops
* **CI Block** (Wedge 4) automatically extracts a lesson into the playbook (Wedge 2).
* **Deprecation Event** (Wedge 1) automatically checks for enforcement rule coverage (Wedge 4).
* **Lock Conflict** (Wedge 3) surfaces coordination opportunities to team leads (Wedge 2).

---

## 🛡️ Enterprise Governance (F1–F7)

Built for teams running multiple AI coding agents (Claude, Cursor, Copilot, custom bots) concurrently:

* **F1 · Cryptographic Identity & Auth:** Every agent authenticates via API keys (`chronos agent create`). `CHRONOS_AUTH=strict` rejects unrecognized callers.
* **F2 · Permission Manifests:** Least-privilege path scoping (e.g. `intern-bot` restricted to `src/payments/**` and denied `src/auth/**`).
* **F3 · Lock TTL & Crash Recovery:** Auto-expiring leases (300s TTL) ensure crashed agents never deadlock a codebase. Emergency locks provide priority preemption.
* **F4 · Tamper-Evident Audit Log:** Append-only SHA-256 hash-chained event ledger for SOC-2 and regulatory audits (`chronos audit verify`).
* **F5 · Human-in-the-Loop Gates:** Protected modules (`.chronos/protected.yml`) park agent write requests until a human approves via `$ chronos gates approve <id>`.
* **F6 · Behavioral Anomaly Detection:** 7-day rolling statistical baselines flag runaway agent loops, volume spikes (>3x), and off-hours modifications.
* **F7 · Sensitive Read Tracking:** Audits agent access to `.env`, credentials, and private keys with full redaction (payload contents are never stored).

---

## 🖥️ Developer Dashboard & Pitch Deck

Chronos includes a built-in read-only developer dashboard and interactive presentation deck:

```bash
chronos dashboard
```

* **Live Dashboard:** `http://127.0.0.1:8080/` — Visualizes active AST locks, provenance churn, recent CI blocks, and governance audits.
* **Presentation Mode:** `http://127.0.0.1:8080/present` — A 13-slide pitch deck pulling real-time live data directly from `chronos.db`.

---

## 💻 CLI Command Reference

```bash
# Health & Diagnostics
chronos doctor                       # Run comprehensive environment & schema health checks
chronos health                       # CI health check (exits non-zero if graph is stale)

# Indexing & Graph Synchronization
chronos --repo . index               # Index repository AST and synchronize temporal graph
chronos --repo . sync                # Sync from existing upstream database
chronos --repo . watch --interval 30 # Continuous filesystem watcher

# CI Merge Gate Enforcement
chronos enforce --diff origin/main --lang python --fail-on-block

# Rule Management & Playbook
chronos approve-rule <rule_id>       # Advance a proposed rule to active enforcement
chronos list-rules                   # View all active and warn-only playbook rules

# Multi-Agent Governance
chronos agent create --name <name>   # Register a new agent and issue an API key
chronos audit verify                 # Verify cryptographic integrity of hash-chained audit log
chronos gates approve <gate_id>      # Unblock a parked human-in-the-loop gate request
```

---

## 🧪 Testing

Run the test suite across all four wedges and governance modules:

```bash
pytest
```

---

## 🔒 Privacy & Local-First Guarantees

* **Zero Network Egress:** The temporal AST graph, intent ledger, and CI enforcer run entirely locally.
* **No LLM in the Core Path:** AST graph sync, node locking, and CI enforcement are 100% deterministic with zero external API dependencies.
* **Auditable State:** All governance events live in a single standard SQLite file (`chronos.db`) backed by a cryptographic hash chain.

---

## 📄 License

MIT © Chronos Contributors. Vendored indexer components licensed under MIT (© 2025 DeusData).
