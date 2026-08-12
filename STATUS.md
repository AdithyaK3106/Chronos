# Chronos — Status

**Updated:** 2026-08-13
**Scope:** Wedge 1 (Bi-Temporal AST Graph). Wedges 2–4 not started.
**Verdict:** Wedge 1 is **functionally complete and verified end-to-end**, with two
gaps that need a design partner rather than more code (below).

---

## Wedge 1 — P0 acceptance

Each row was executed, not reasoned about. Evidence is what the command actually printed.

| Req | Status | Evidence |
|---|---|---|
| **P0-1** Adopt upstream as parsing engine | ✅ | Built from vendored source (v0.10.3). Indexed 3 repos. Chronos re-parses nothing. |
| ↳ ingests parse success/failure counts | ✅ | `health.coverage` → `{parse_partial: 2, not_indexed_file: 7, not_indexed_dir: 7}` |
| ↳ no silent divergent fork | ✅ | Submodule pinned at `70a9539`; zero patches applied. |
| **P0-2** Sync layer → Graphiti bi-temporal | ✅ | Rename produced `added=4 invalidated=5 unchanged=178`. |
| ↳ superseded nodes stay queryable | ✅ | `callers(changes)` before rename → `[main, what_changed]`; after → none. |
| ↳ current queries never return invalidated edges | ✅ | Enforced in the query predicate; covered by the suite. |
| ↳ stateless, re-derivable glue | ✅ | Identity is `uuid5(group, qualified_name)`. No Chronos-side state exists. |
| **P0-3** Incremental re-index | ✅ | Unchanged re-index → `added=0 invalidated=0 unchanged=183`. |
| ↳ never requires full re-sync | ✅ | Same run: only the delta is written. |
| **P0-4** Unified MCP interface | ⚠️ partial | 5 tools live and answering. Current-state proxying not built — see gaps. |
| ↳ explicit no-data, never silent fallback | ✅ | Pre-history → `"predates the graph's earliest record"`; unknown symbol → `"not present"`. |
| **P0-5** Self-hosted, bundled | ✅ | Embedded Kuzu, no server. Sync+query completed with **all external sockets raising**. |
| ↳ one install step | ✅ | `submodule update` → `pip install -e .` → `build_cbm` (verified from a fresh clone). |
| **P0-6** Index health | ✅ | `chronos health` returns status/freshness/coverage; exits 1 when not fresh. |

**Suite:** `python test_chronos.py` → ALL PASS (14 checks, incl. supersession,
idempotency, identity collisions, no-data signalling, coverage).

---

## Verified against real repos

Not fixtures — third-party projects indexed with the locally-built indexer.

| Repo | Language | Result |
|---|---|---|
| Shiplog | TS/JS (347 files) | 379 nodes, 143 edges, 12.4s → 111 facts. `createClient` → **13 callers** across pages/auth/routes. |
| Setu | TS/JS (168 files) | 1502 nodes, 322 edges, 14.1s → 297 facts. `getActor` → 33 callers. |
| Chronos itself | Python | 193 nodes. Live rename verified before/after temporal state. |

**Fresh-clone build:** `python -m chronos.build_cbm` → 5m03s, 295 MB, exit 0,
then indexed a repo it had never seen.

---

## Performance

| Metric | Value | Note |
|---|---|---|
| Sync throughput | **~2,100 writes/sec** | Was 30/s. Cause was serializing 1024-float embedding vectors, found by measurement — not transaction overhead, which was my first (wrong) hypothesis. |
| 5k nodes + 20k edges | **12s** | Previously exceeded a 10-minute timeout. |
| No-op re-sync | 2.1s | Content-hash short-circuit. |
| Query p50 | 58ms | Well inside the sub-second p95 target. |
| Index (350-file repo) | 12–14s | Upstream's C indexer. |

P0-3's 5-minute SLA holds with large margin at these sizes. **Not yet load-tested
at multi-million LOC**, which the v1 PRD explicitly flags as an open question.

---

## Gaps

**1. Current-state proxying (P0-4, partial).** The PRD asks for current call graph
and current impact "proxied from Codebase-Memory MCP" behind one MCP server. Chronos
exposes the 4 temporal tools + health; for current state, upstream's own MCP server
runs alongside. This was deliberate — proxying 15 upstream tools we'd add nothing to
is surface area for its own sake — but it is a **documented deviation, not a
completed requirement**. The PRD lists "mirror 1:1 or ship an opinionated set?" as an
open product question; this is a bet on the latter and should be confirmed with a
partner.

**2. Non-Windows build unverified.** `_run_posix` is the simpler branch (plain
`make`, no MSYS2 path translation or `TMPDIR` workaround) but has never executed
here. One Linux CI run settles it.

**3. Scale.** Largest real index is 1502 nodes. Behavior on a multi-million-LOC
monorepo is unmeasured, and Kuzu is marked deprecated upstream — isolated to
`store.py`, and `CHRONOS_DB_URI` already routes to Neo4j when a partner outgrows it.

---

## Notable decisions

**No LLM in the write path.** Graphiti's `add_episode`/`add_triplet` run LLM entity
extraction and embedding dedup per fact. AST facts are already structured, and fuzzy
dedup would merge distinct same-named functions. Chronos writes `EntityNode`/
`EntityEdge` via their documented `.save()`, which sets `valid_at`/`invalid_at`
natively. This is what makes "no external services" literally true.

**Identity = upstream's `qualified_name`.** Testing on a real repo (not fixtures)
found `path::name::kind` collided **6 times in 379 nodes** — nested closures, and
folders sharing a basename with files. `qualified_name` collided 0 times and is
`UNIQUE` per project upstream. This mattered: colliding identity merges distinct
functions into one temporal history and yields confidently wrong as-of answers.

**Vendored, not copied.** The cross-file type resolution that distinguishes
`client.send()` from `server.send()` spans `src/pipeline/` + `internal/cbm/`
(~292k lines of C). Copying a subset yields a parser without the resolution;
copying all of it is a fork. Built from a pinned submodule instead, so upstream
fixes arrive as a `git pull`.

---

## Next

- **Blocking on nobody:** Linux CI build; load test on a large monorepo.
- **Needs a partner:** confirm the opinionated-vs-1:1 MCP tool surface (gap 1).
- **Per platform PRD sequencing:** Wedge 3 (intent ledger) is next, gated on a
  spike of Forge Orchestrator's extensibility to AST-node granularity. Wedge 1's
  graph is the dependency it needs, and that now exists.
