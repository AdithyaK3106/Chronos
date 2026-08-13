# Chronos v1 — Bi-Temporal AST Knowledge Graph

Gives AI coding agents a memory of what your codebase **used to** look like, so
they stop confidently reintroducing patterns you deleted last month.

Current-state tools answer "what calls `foo`?". Chronos also answers
**"what called `foo` before last week's refactor?"** — and returns an explicit
*no data* when it doesn't know, instead of silently falling back to current state.

```
$ chronos --db ~/.cache/codebase-memory-mcp/graph.db --repo . sync
synced myrepo @ 2026-08-12T17:34:26+00:00 in 0.8s: nodes=46 added=4 invalidated=6 unchanged=74
```

```
callers of health()               → before refactor: [main, do_doctor, do_health]
                                  → now:             NONE (superseded)
callers of index_health_report()  → before refactor: NONE (did not exist)
                                  → now:             [do_health, do_doctor]
```

## Install

```bash
git submodule update --init --depth 1   # vendored indexer source (MIT)
pip install -e .
python -m chronos.build_cbm             # build the indexer (~5 min, needs a C toolchain)
```

Embedded graph store (Kuzu), no server, **no LLM API key, no network egress.**
The indexer is built from vendored source — no downloaded binary at runtime.
On Windows the build needs MSYS2 (`pacman -S mingw-w64-ucrt-x86_64-gcc make`);
`chronos doctor` reports what's missing.

## Use

```bash
chronos doctor                       # verify toolchain, build state, schema mapping
chronos --repo . index               # index with the vendored indexer, then sync
chronos --repo . sync                # sync from an existing upstream db
chronos --repo . watch --interval 30 # continuous (P0-3)
chronos health                       # exit 1 if graph is not fresh (CI-friendly)
```

Two input paths, same schema: `index` runs the vendored indexer ourselves;
`sync` reads whatever SQLite graph is already on disk (`upstream.py`), so an
externally-installed codebase-memory-mcp still works unchanged.

Register the MCP server with your agent:

```json
{ "mcpServers": {
    "chronos": {
      "command": "chronos-mcp",
      "env": { "CHRONOS_GROUP_ID": "myrepo" }
    },
    "codebase-memory": {
      "command": "vendor/codebase-memory-mcp/build/c/codebase-memory-mcp"
    } } }
```

Two servers by design: **Chronos answers temporal questions, upstream answers
current-state ones.** Chronos does not proxy upstream's tools — it would be a
pass-through for queries it doesn't own, and agents already handle multiple MCP
servers. See `docs/STATUS.md` D-1.

Chronos tools: `as_of_callers`, `as_of_callees`, `as_of_impact`, `what_changed`,
`index_health`. Times accept ISO-8601, `now`, or relative (`7d`, `12h`).

### Intent locks & provenance (Wedge 3)

Concurrent agents declare intent on a node before touching it, and every change is
stamped with who made it and why. Node ids are Wedge 1 identities, so a lock names
the same symbol the temporal graph does — locking is per-function, not per-file.

```json
{ "mcpServers": { "chronos-ledger": { "command": "chronos-ledger-mcp" } } }
```

`chronos_acquire_lock`, `chronos_release_lock`, `chronos_check_conflicts`,
`chronos_log_provenance`, `chronos_who_touched`.

```
acquire(createClient, agent-a, "refactor to async")  → acquired
acquire(createClient, agent-b, "add retry logic")    → conflict: held by agent-a,
                                                       intent "refactor to async"
```

Locks carry a TTL (default 300s) and expired ones are swept on the next
acquisition, so a crashed agent can't wedge a node — no background process.
`agent_id`/`session_id` are caller-supplied strings: they identify, they do not
authenticate. This prevents collisions between cooperating agents, not hostile
ones.

## How it fits together

```
codebase-memory-mcp  ──SQLite(ro)──>  chronos sync  ──>  Graphiti/Kuzu  ──>  MCP tools
   (current truth)                    (stateless)        (history)          (agents)
```

Upstream owns current structure; Graphiti owns history. The sync layer holds **no
state of its own** — UUIDs are derived deterministically (`uuid5`) from structural
identity, so the store is fully re-derivable if the process dies mid-run (P0-2).

Run upstream's own MCP server alongside this one for current-state search; Chronos
deliberately does not proxy its 15 tools.

## Key design decisions

**No LLM in the write path.** Graphiti's `add_episode`/`add_triplet` run LLM entity
extraction and embedding dedup on every fact. AST facts are already structured —
there is nothing to extract, and fuzzy dedup would merge distinct same-named
functions. Chronos writes `EntityNode`/`EntityEdge` via their documented `.save()`,
which sets `valid_at`/`invalid_at` natively. This is what makes P0-5's "no external
services" true; it is also ~free and deterministic. No fork of either project.

**Identity excludes line numbers.** A function shifting down 10 lines is not a new
function; including line numbers would churn history on every edit.

**`valid_at` = git commit time, not wall clock,** so re-syncing an old checkout
can't claim to be current.

**Superseded facts are closed, never deleted** (`invalid_at` set), which is what
makes as-of queries work.

## Status

Verified working end-to-end: schema discovery, sync, supersession, idempotent
re-sync, as-of queries across a real refactor of this repo, and all 5 MCP tools.

```bash
python tests/test_chronos.py   # bi-temporal contract self-check
python tests/test_wedge3.py    # intent locks + provenance
```

Validated against the real indexer built from vendored source (commit `70a9539`)
on a third-party TypeScript/JS repo: 379 nodes, 143 edges, 12.4s index + sync,
with `createClient` correctly resolving 13 callers across pages, auth, and routes.

`chronos/upstream.py` maps the SQLite schema by runtime introspection rather than
hardcoding it, and is the only place upstream schema knowledge lives. `chronos
doctor` prints the resolved mapping and fails loudly with the table list if it
can't map.

## Vendoring

`vendor/codebase-memory-mcp` is a git submodule pinned to a specific upstream
commit (MIT, © 2025 DeusData — license preserved in the submodule). We build it
from source rather than copying its C into this repo: the cross-file type
resolution that distinguishes `client.send()` from `server.send()` spans
`src/pipeline/` plus `internal/cbm/` (~292k lines), so copying a subset yields a
parser without the resolution, and copying all of it is a fork. Upgrades are an
explicit submodule bump; upstream fixes arrive as a `git pull`, not a re-port.

## Not built (deferred, per PRD)

Wedges 2–4 (ACE playbook, intent ledger, CI enforcement), multi-repo linking,
branch-aware indexing, query caching, Slack alerts, hosted SaaS.
