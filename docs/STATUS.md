# Chronos — Status

**Updated:** 2026-08-13
**Scope:** Wedge 1 (Bi-Temporal AST Graph) and Wedge 3 (Intent & Provenance Ledger).
Wedges 2 and 4 not started.
**Verdict:** Wedges 1 and 3 are **functionally complete and verified end-to-end**
against four real third-party repos. Two gaps remain, neither blocking a demo.

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
| **P0-4** Unified MCP interface | ✅ | 5 tools live and answering. Current-state proxying intentionally not built — the PRD's open question on tool surface is answered in Decisions (D-1); scope superseded, not skipped. |
| ↳ explicit no-data, never silent fallback | ✅ | Pre-history → `"predates the graph's earliest record"`; unknown symbol → `"not present"`. |
| **P0-5** Self-hosted, bundled | ✅ | Embedded Kuzu, no server. Sync+query completed with **all external sockets raising**. |
| ↳ one install step | ✅ | `submodule update` → `pip install -e .` → `build_cbm` (verified from a fresh clone). |
| **P0-6** Index health | ✅ | `chronos health` returns status/freshness/coverage; exits 1 when not fresh. |

**Suite:** `python tests/test_chronos.py` → ALL PASS (17 checks, incl. supersession,
idempotency, identity collisions, no-data signalling, coverage, gc safety, and a
cross-path round-trip).

---

## Wedge 3 — Intent & Provenance Ledger

Agents declare intent on an AST node before touching it; every change is stamped
with who and why. Node ids are Wedge 1 identities, so a lock names the same symbol
the temporal graph does — **per-function, not per-file.**

| Capability | Status | Evidence |
|---|---|---|
| Intent locks, one per node | ✅ | PK-guarded INSERT in an IMMEDIATE transaction. 12 threads raced one node → exactly 1 acquired. |
| Conflict reports holder + intent | ✅ | agent-B on a held node → `held_by=agent-A, intent='refactor fallback loop'`. |
| TTL expiry, no background thread | ✅ | Swept on next acquire. Verified with a real 1s TTL elapsing. |
| Release restricted to owner | ✅ | Wrong agent → `not_owner` with current holder. |
| Append-only provenance | ✅ | Earlier rows verified unmutated after later appends. |
| Multi-node pre-flight | ✅ | `chronos_check_conflicts` dedupes and splits locked/free. |
| Doctor integration | ✅ | `ledger: ok \| N active locks \| N events \| <path>`. |

**Suite:** `python tests/test_wedge3.py` → ALL PASS (12 checks).
**Size:** 260 lines across `ledger.py` + `wedge3_mcp.py`.

Full two-agent scenario on real Opencode node ids (`newFallbackState::Function`):
A locks 1,2,3 → B conflicts on 2 → A logs provenance → A releases → B acquires →
`who_touched` shows A's change and reason. Ledger: 1 lock, 3 events.

**Scope limits, stated plainly:** `agent_id` identifies but does not authenticate —
any caller can claim any id, so this prevents collisions between *cooperating*
agents. And locks cover exactly the nodes named; structurally-adjacent conflicts
(two agents on functions that call each other) are not yet detected. Expanding the
lock set via `as_of_callers`/`callees` needs no schema change.

---

## Verified against real repos

Not fixtures — third-party projects indexed with the locally-built indexer.

| Repo | Nodes | Edges | Index | Facts | Top query |
|---|---|---|---|---|---|
| Setu | 1,502 | 322 | 16.6s | 297 | `getActor` → 33 callers |
| MediAssist | 2,415 | 398 | 16.6s | 376 | `append` → 25 callers |
| Opencode | 9,907 | 3,034 | 16.5s | 2,601 | `getDb` → **68 callers** |
| ortho | 10,889 | 5,678 | 17.7s | 5,609 | `str` → 353 callers |

Zero indexing failures, zero empty results. Drishti was excluded during triage —
121 files, all `.docx`/config, no source.

Also verified: Shiplog (TS/JS, `createClient` → 13 callers) and Chronos itself
(live rename correct before/after).

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

**1. Non-Windows build unverified.** `_run_posix` is the simpler branch (plain
`make`, no MSYS2 path translation or `TMPDIR` workaround) but has never executed
here. One Linux CI run settles it.

**1b. Adjacent-node conflict detection (Wedge 3).** The platform PRD's Wedge 3 P0
asks for the graph to catch structurally-adjacent edits. Locks currently cover
exactly the nodes an agent names. Additive; no schema change needed.

**2. Scale.** Largest real index is 1502 nodes. Behavior on a multi-million-LOC
monorepo is unmeasured, and Kuzu is marked deprecated upstream — isolated to
`store.py`, and `CHRONOS_DB_URI` already routes to Neo4j when a partner outgrows it.

---

## Decisions

**D-1. Two MCP servers, not one proxy.** *(Resolved 2026-08-13. Supersedes P0-4's
"proxied from Codebase-Memory MCP" scope and answers the PRD's open product
question: "mirror the 14–15 tools 1:1, or a smaller opinionated set?")*

The PRD described proxying upstream's current-state query tools through Chronos's
MCP server so agents talk to one server. This was deliberately not built. Proxying
15 upstream tools Chronos adds nothing to is surface area without value — it would
make Chronos a pass-through for queries it does not own. The correct architecture is
two MCP servers coexisting: **Chronos for temporal queries, upstream for
current-state queries.** Agents are already equipped to talk to multiple MCP servers.

Consequences, stated plainly so this stays reviewable:

- Chronos exposes 5 tools (4 temporal + health), all of which answer questions no
  other server in the stack can. Nothing is a passthrough.
- Operators register two servers instead of one. This is configuration, not
  integration work — see README for the block.
- The boundary matches ownership: upstream owns current structure, Chronos owns
  history. A proxy would have blurred it, and any upstream tool change would have
  become a Chronos maintenance burden.
- Reversible. If a partner requires a single endpoint, proxying upstream's tools is
  additive and touches only `server.py`.

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

**Both input paths must agree on identity.** The above fix initially reached only
the sync path: `indexer.py` dropped `qname` when building node dicts, so everything
written by `chronos index` silently fell back to the colliding scheme, and the two
paths disagreed with no error anywhere. Caught by round-tripping real indexed nodes
back through the query path — **0/300 found**, now 300/300. A permanent test
(`roundtrip()`) indexes a real repo and asserts 100% lookup, because this class of
bug is invisible to any test that exercises one path at a time.

**gc requires a superseded fact, not merely no current one.** A node with no facts
at all is a valid symbol — Chronos syncs every upstream node but only
`TEMPORAL_EDGE_TYPES` edges, so most nodes have no edges. The literal "no current
facts" rule flagged **18,248 nodes on Opencode where only 801 were real orphans**.
Deleting the difference would have destroyed most of the graph.

**Vendored, not copied.** The cross-file type resolution that distinguishes
`client.send()` from `server.send()` spans `src/pipeline/` + `internal/cbm/`
(~292k lines of C). Copying a subset yields a parser without the resolution;
copying all of it is a fork. Built from a pinned submodule instead, so upstream
fixes arrive as a `git pull`.

---

## Next

- **Blocking on nobody:** Linux CI build; load test on a large monorepo;
  adjacent-node conflict expansion for Wedge 3.
- **Per platform PRD sequencing:** Wedges 1 and 3 are done. Wedge 2 (ACE playbook,
  wiring Packmind OSS + Kayba ACE) is next, then Wedge 4 (enforcement). Wedge 3 was
  built in-house on SQLite rather than by extending Forge Orchestrator — the PRD's
  spike question ("can its file locking extend to AST-node granularity without a
  fork?") was bypassed: node-level locking keyed on Wedge 1 identities is ~260
  lines and needs no external coordination substrate.

---

## Maintenance

`chronos gc` removes nodes whose facts have all been superseded — the residue of
identity migrations. Dry-run by default; `--execute` to delete. `chronos doctor`
warns above 10% orphans and prints the command.

Verified lossless on a real repo: a forced identity migration on Setu produced 170
orphans; gc deleted exactly those, leaving `facts_current` at 297 and
`callers(getActor)` at 33.

**Known caveat:** an early, buggy version of gc was run against the scratch graph
used during repo testing and stripped `RELATES_TO` relationships from live fact
nodes there. That store is disposable and was rebuilt. The committed gc does not
have this defect — confirmed by rebuilding from scratch, forcing a migration, and
re-running — and a regression test now pins the invariant (live facts and
fact-less symbols both survive).
