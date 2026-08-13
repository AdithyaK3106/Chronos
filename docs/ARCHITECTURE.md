# Chronos — Architecture

One MCP server, four capability layers, two data stores. This document covers
how the parts connect and why the boundaries fall where they do.

---

## 1. Data flow

```
                          ┌─────────────────────────────┐
   Agent ────MCP────────► │  chronos-mcp  (19 tools)    │
   (Claude Code,          │  chronos/server.py          │
    Cursor, CI)           └──────────────┬──────────────┘
                                         │
              ┌──────────────┬───────────┴────┬─────────────────┐
              ▼              ▼                ▼                 ▼
        ┌───────────┐  ┌───────────┐   ┌───────────┐     ┌───────────┐
        │  Wedge 1  │  │  Wedge 3  │   │  Wedge 2  │     │  Wedge 4  │
        │  temporal │  │  intent   │   │  policy   │     │    CI     │
        │   graph   │  │  ledger   │   │  playbook │     │ enforce   │
        └─────┬─────┘  └─────┬─────┘   └─────┬─────┘     └─────┬─────┘
              │              │               │                 │
              ▼              ▼               ▼            ┌────┴────┐
        ┌──────────┐   ┌──────────────────────────┐       ▼         ▼
        │ Graph DB │   │      chronos.db          │   ast-grep    OPA
        │  Kuzu /  │   │  (SQLite, one file)      │   (MIT)    (Apache-2)
        │  Neo4j   │   │  intent_locks            │
        └────▲─────┘   │  provenance_events       │
             │         │  enforcement_rules       │
             │         └──────────────────────────┘
             │                     ▲
        ┌────┴─────┐               │
        │   cbm    │          litellm ──► LLM ──► Packmind
        │ (C, sub- │                              (rule store,
        │ process) │                               Apache-2)
        └──────────┘
```

Reading it: agents talk to one server. Wedge 1 owns the graph; Wedges 2–4 share
`chronos.db`. Only Wedge 2 and Wedge 4's rule generator reach an LLM, and only
Wedge 2 reaches Packmind. Wedge 1's indexer and Wedge 4's matchers are
subprocesses.

---

## 2. Cross-wedge triggers

The wedges are not independent tools — they feed each other. All three triggers
are **best-effort**: a trigger failure is logged and swallowed, never propagated
to the originating operation.

| Event | Source | Target | Action | Failure behavior |
|---|---|---|---|---|
| CI block | 4 | 2 | Auto-reflect the block into a candidate rule, then curate | Log; **enforcement verdict is unaffected** |
| Node deprecated | 1 | 4 | Warn if no active rule covers the node | Log only; sync completes normally |
| Lock conflict | 3 | 2 | Log a coordination-lesson hint | Log only; lock semantics unchanged |

Disable all three with `CHRONOS_AUTO_TRIGGERS=false`.

**Why trigger 1 is automatic.** The manual path (`chronos_capture_lesson`)
requires an agent to notice it failed and choose to report it. The agents most
worth learning from are precisely the ones that confidently did the wrong thing
and will not self-report. A CI block is a labelled failure — an action, a stated
reason, and a known node — already in the exact shape the Reflector wants. That
makes it the highest-signal event in the system, and the only one worth
capturing without being asked.

**Why trigger 2 warns instead of acting.** The most common Chronos failure mode
is a node being deprecated in the graph with no enforcement rule covering it:
agents keep using the superseded symbol and Wedge 4 passes, because a rule that
does not exist cannot fire. This is silent, which makes it dangerous. But
generating a rule costs an LLM call and needs human approval, so the trigger
surfaces the gap at the moment of deprecation and stops there.

**Why trigger 3 does not reflect at all.** Conflicts are weak signal next to CI
blocks. Two agents wanting the same node is often legitimate concurrency, not a
mistake. Auto-reflecting every conflict would flood the playbook with noise and
train the Curator's dedup against us. Informational only.

---

## 3. Data stores, and why there are exactly two

**`chronos.db` (SQLite)** — locks, provenance, enforcement rules.

Before unification these were nominally separate concerns and the file was named
`ledger.db` after one wedge. One file, one set of PRAGMAs, one path to back up,
one thing to name when a partner asks where the data is. `chronos/db.py` is the
only module that opens it.

**The graph store (Kuzu, or Neo4j via `CHRONOS_DB_URI`)** — the bi-temporal AST
graph.

Deliberately *not* merged into `chronos.db`. It is a different engine serving a
different query pattern: multi-hop traversal with temporal predicates, which is
what Graphiti is built on. Folding it into SQLite would mean replacing Graphiti
— a wedge-level rewrite, not a packaging change. The boundary is engine-shaped,
not organizational, which is why it survives unification.

Everything else that looks like state is derived and disposable: `.chronos/rules/*.yml`
is regenerated from `enforcement_rules`, and the upstream SQLite index is owned
by codebase-memory-mcp and opened read-only.

---

## 4. What Chronos does not do

Stated so partners know where the product ends:

- **Not a competing agent tool.** Chronos gives agents memory and guardrails; it
  does not write code or replace Claude Code, Cursor, or Copilot.
- **Not a hosted SaaS.** Self-hosted only. No telemetry, no account, no egress
  beyond the LLM and Packmind endpoints you configure.
- **Not a Slack/Jira/ticket ingestion system.** It reads code and CI outcomes,
  not conversations.
- **Not a general-purpose knowledge graph.** The graph holds AST structure and
  its history. It is not a place to put documents, embeddings, or prose.
- **Not a linter.** Wedge 4 gates on rules the team accumulated and a human
  promoted, confirmed against the temporal graph. It has no opinion on style.

---

## 5. Subprocess dependencies

Three external binaries, each called via subprocess rather than bindings.

**`cbm` (codebase-memory-mcp's C indexer)** — cross-file type resolution, the
thing that distinguishes `client.send()` from `server.send()`. No Python
equivalent exists. Vendored as a pinned git submodule and built from source, so
upstream fixes arrive as a `git pull` rather than a re-port. Copying a subset of
its ~292k lines would yield a parser without the resolution; copying all of it
would be a fork.

**`ast-grep` (MIT)** — structural pattern matching for Wedge 4. Subprocess
because the Python bindings are thin wrappers over the same CLI, and a
subprocess pins us to the exact version we tested against (0.45.1) rather than
whatever the bindings resolve to. Note that `sg` is deprecated upstream; Chronos
invokes `ast-grep`.

**`opa` (Apache 2.0)** — Rego policy evaluation. Subprocess because the policy
is a real file (`chronos/policies/enforce.rego`) meant to be audited, diffed,
and edited by whoever owns enforcement — not a string embedded in Python. Note
that OPA ≥ 1.0 requires Rego v1 syntax; the shipped policy is v1.

Both `ast-grep` and `opa` fail loudly with their install command when missing.
Enforcement is never silently skipped — a CI check that quietly stops checking
is worse than one that fails.

---

## 6. Module map

| Module | Wedge | Role |
|---|---|---|
| `server.py` | all | The MCP server. Registers all 19 tools. |
| `wedge1_mcp.py` | 1 | Temporal query tools |
| `upstream.py` / `indexer.py` / `build_cbm.py` | 1 | Read and build the upstream index |
| `sync.py` / `store.py` / `query.py` | 1 | Graph writes, driver, as-of reads |
| `wedge3_mcp.py` / `ledger.py` | 3 | Intent locks, provenance |
| `wedge2_mcp.py` / `reflector.py` / `curator.py` / `playbook.py` | 2 | Lesson capture, curation, Packmind |
| `wedge4_mcp.py` / `rule_generator.py` / `detectability.py` / `enforcer.py` / `rule_store.py` | 4 | Rule generation, validation, enforcement |
| `db.py` | 2/3/4 | The single SQLite connection manager |
| `triggers.py` | cross | The three cross-wedge triggers |
| `legacy_entry.py` | — | Deprecated per-wedge MCP aliases |
