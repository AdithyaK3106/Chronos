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

## Architecture

Chronos runs as a **single MCP server (`chronos-mcp`) with one config line**.
Internally it is four capability layers that build on each other:

| Layer | Knows |
|---|---|
| **Wedge 1** — Temporal Graph | what the codebase looked like at any point in time |
| **Wedge 3** — Intent Ledger | what agents are about to change, before they change it |
| **Wedge 2** — Policy Playbook | what the team's standards are, and updates them automatically |
| **Wedge 4** — CI Enforcement | blocks merges that violate standards or use deprecated patterns |

These are not independent tools. They share one data store, one MCP server, and
automatic feedback loops:

- A **CI block** (Wedge 4) automatically feeds a lesson into the playbook (Wedge 2)
- A **deprecation event** (Wedge 1) automatically checks for enforcement coverage (Wedge 4)
- A **lock conflict** (Wedge 3) surfaces a coordination-lesson opportunity (Wedge 2)

Triggers are best-effort: a trigger failure never affects the operation that
fired it — a CI block stands even if the Reflector throws. Disable them all with
`CHRONOS_AUTO_TRIGGERS=false`. Full detail in [docs/ARCHITECTURE.md](ARCHITECTURE.md).

## Setup (one command)

```json
{ "mcpServers": {
    "chronos": {
      "command": "chronos-mcp",
      "env": {
        "CHRONOS_GROUP_ID": "myrepo",
        "CHRONOS_LLM_MODEL": "openai/gpt-4o-mini"
      }
    } } }
```

That is the whole config — 19 tools across all four wedges. Ready to copy from
[docs/mcp-config.json](mcp-config.json).

Wedge 1 tools: `as_of_callers`, `as_of_callees`, `as_of_impact`, `what_changed`,
`index_health`. Times accept ISO-8601, `now`, or relative (`7d`, `12h`).

Run upstream's `codebase-memory-mcp` alongside it for current-state structural
search — Chronos answers temporal questions and deliberately does not proxy
upstream's 15 tools (`docs/STATUS.md` D-1).

> The per-wedge commands (`chronos-graph-mcp`, `chronos-ledger-mcp`,
> `chronos-playbook-mcp`, `chronos-enforce-mcp`) still work as deprecated
> aliases — each starts the same unified server and prints a notice. They will
> be removed in a future release.

## Data

All state lives in two places, both under `~/.chronos/` by default:

- **`chronos.db`** — locks, provenance, enforcement rules (SQLite). Back this up.
- **`graph.kz`** — the temporal AST graph (Kuzu). Back this up.

Two stores, not one, because the graph is a different engine serving multi-hop
temporal traversal; merging it would mean replacing Graphiti. Everything else is
one file so there is one path to name and one thing to back up. An existing
`ledger.db` is migrated into `chronos.db` automatically on first connection and
kept as `ledger.db.bak`.

### Intent locks & provenance (Wedge 3)

Concurrent agents declare intent on a node before touching it, and every change is
stamped with who made it and why. Node ids are Wedge 1 identities, so a lock names
the same symbol the temporal graph does — locking is per-function, not per-file.

These ship on the unified `chronos-mcp` server — no extra config.

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

### Self-improving playbook (Wedge 2) — partly verified

> The **Reflector is live-verified**: a real LLM call produced a rule grounded in
> real temporal evidence from the graph. The **Curator and the Packmind HTTP
> layer are not** — the latter is written from reading Packmind's source and has
> never seen a response. Treat rule extraction as working and submission as
> unproven until `docs/wedge2-setup.md` has been walked end to end;
> `docs/STATUS.md` lists exactly what that leaves at risk.

Agent gets corrected → Reflector extracts a candidate rule, grounded in Wedge 1
history ("this broke on a node refactored 2 weeks ago") → Curator dedups and
quality-gates it → the rule is created in Packmind, unpublished, for a human to
approve. Static CLAUDE.md files rot; this one updates itself from real mistakes.

These ship on the unified `chronos-mcp` server — no extra config.

`chronos_capture_lesson`, `chronos_query_playbook`, `chronos_propose_rule`,
`chronos_playbook_health`. Needs a running Packmind and an LLM —
see `docs/wedge2-setup.md`. Packmind (Apache 2.0) is the rule store; Chronos does
not reimplement storage, versioning, or distribution.

**Approval gate:** Packmind's OSS API has no proposal/status field, but creating
and *publishing* a standard are separate calls, and only publishing writes
CLAUDE.md. Chronos never publishes — proposed rules sit inert until a human
approves them in the UI.

### CI enforcement (Wedge 4)

Turns a playbook rule into an ast-grep pattern, and gates merges on it. A rule
blocks only when **both** a human promoted it *and* the temporal graph confirms
the matched symbol is actually superseded — a pattern match alone is a warning.
That is the difference from every static linter: the deprecation list is the live
graph, not a hand-maintained file.

```bash
pip install ast-grep-cli          # MIT
# plus the OPA binary (Apache 2.0) — see docs/wedge4-ci.yml
chronos enforce --diff origin/main --lang typescript --fail-on-block
```

These ship on the unified `chronos-mcp` server — no extra config.

`chronos_generate_rule`, `chronos_enforce`, `chronos_promote_rule`,
`chronos_list_rules`, `chronos_rule_report`. Every generated rule starts
warn-only and must pass a detectability check (does it catch its own example?)
before it can be promoted. Blocks are stamped into the Wedge 3 ledger, so each
one is traceable to an agent, a session, and a rule.

The decision logic is `chronos/policies/enforce.rego` — a real OPA policy file,
editable and auditable without touching Python.

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

## Layout

```
chronos/      upstream.py  read upstream's SQLite (schema by introspection)
              indexer.py   run the vendored indexer   build_cbm.py  build it
              sync.py      upstream rows -> bi-temporal nodes/edges
              store.py     Kuzu/Neo4j driver          query.py  as-of reads
              cli.py       chronos                    wedge1_mcp.py  its tools
              server.py    THE MCP server (all 19)     db.py  one SQLite handle
              triggers.py  cross-wedge feedback loops
              ledger.py    intent locks + provenance  wedge3_mcp.py  its server
              reflector.py trace -> candidate rule    curator.py  gate + submit
              playbook.py  Packmind REST client       wedge2_mcp.py  its server
              rule_generator.py rule -> ast-grep      detectability.py  validate
              enforcer.py  ast-grep + OPA + stamp     rule_store.py  lifecycle
              policies/enforce.rego  the decision     wedge4_mcp.py  its server
docs/         STATUS.md (what's verified, decisions), prd-v1.md, prd-platform.md
tests/        test_chronos.py (bi-temporal contract), test_wedge2.py (playbook),
              test_wedge3.py (ledger), test_wedge4.py (enforcement),
              test_unification.py (one server, one db, triggers)
vendor/       codebase-memory-mcp submodule, pinned
```

Dependencies run one way: `cli`/`server` → `query`/`sync` → `store`/`upstream`.
The ledger is independent of the graph — it shares only node identity strings.

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

Multi-repo linking, branch-aware indexing, query caching, Slack alerts, hosted
SaaS. All four wedges are built; see `docs/STATUS.md` for what is verified live
versus against mocks.
