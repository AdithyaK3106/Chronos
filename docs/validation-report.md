# Chronos Validation Report

**Date:** 2026-08-13
**Repos tested:** Setu, MediAssist, Opencode, ortho
**Raw metrics:** every number below comes from a recorded run (`results.json`),
not from memory or recollection.

---

## Executive Summary

Chronos works end-to-end across all four repos. 24,713 nodes and 9,432 edges
were indexed and synced with **zero indexing failures and zero errors** in any
section; temporal queries answered in **7–56 ms** (mean 20 ms), intent locks in
**0.4–2.1 ms**, and the full Wedge 3 two-agent concurrency scenario passed on
every repo with correct conflict attribution.

The headline caveat is a **negative result that turned out to be correct
behavior**: the enforcement run produced **zero blocks** on real repo files. The
cause is not a bug — nothing in a freshly-synced graph is deprecated, and OPA
refuses to block without temporal confirmation. A follow-up test that forced a
genuine supersession produced a real `block` verdict, stamped into the ledger,
confirming the path works. Enforcement is therefore *safe by default* to a
degree worth stating plainly: **a partner who installs Chronos and promotes a
rule will still see no blocks until the graph has history.**

Two real problems surfaced. **Cross-wedge Trigger 1 adds ~38x latency to a
blocking enforcement call** (5,099 ms vs 134 ms) because it makes a synchronous
LLM call in the enforcement path. And **node language is never populated** —
every node in all four repos reports `language: '?'`, which makes the
per-language rule filtering in Wedge 4 effectively a no-op.

---

## Repo Index Summary

| Repo | Nodes | Edges | Facts | Index time | Sync time | Nodes/sec | Facts/sec |
|---|---|---|---|---|---|---|---|
| Setu | 1,502 | 322 | 297 | 13.85s | 0.77s | 108 | 386 |
| MediAssist | 2,415 | 398 | 376 | 18.79s | 1.13s | 129 | 333 |
| Opencode | 9,907 | 3,034 | 2,601 | 14.83s | 5.66s | 668 | 460 |
| ortho | 10,889 | 5,678 | 5,609 | 15.31s | 8.52s | 711 | 658 |
| **Total** | **24,713** | **9,432** | **8,883** | **62.8s** | **16.1s** | — | — |

Zero indexing failures. Zero orphaned nodes in any repo (0.0%), so no `gc`
warning triggered anywhere.

**Node kinds** (Setu, representative): Method 318, Field 318, File 173, Module
165, Class 140, Section 123, Function 111, Variable 74, Folder 42, Decorator 17,
Interface 12.

**Language breakdown: not obtainable.** The indexer emits no `language` field on
nodes (keys are `kind`, `name`, `path`, `qname` only), so the per-language node
counts this section asked for cannot be produced from the graph. See Failures #1.

---

## Temporal Query Results

| Repo | Query | Symbol | Result count | Latency |
|---|---|---|---|---|
| Setu | `as_of_callers` now | `getActor` | 33 | 18.9 ms |
| Setu | `as_of_callers` 30d | `getActor` | 0 — *explicit no-data* | 16.8 ms |
| Setu | `as_of_callees` now | `update` | 22 | 7.8 ms |
| Setu | `what_changed` 7d | — | 0 | 14.0 ms |
| Setu | `as_of_impact` | `getActor` | 10 | 24.5 ms |
| MediAssist | `as_of_callers` now | `append` | 25 | 17.6 ms |
| MediAssist | `as_of_callers` 30d | `append` | 0 — *explicit no-data* | 16.9 ms |
| MediAssist | `as_of_callees` now | `get` | 28 | 7.2 ms |
| MediAssist | `what_changed` 7d | — | 0 | 12.5 ms |
| MediAssist | `as_of_impact` | `append` | 40 | 25.2 ms |
| Opencode | `as_of_callers` now | `getDb` | **68** | 10.3 ms |
| Opencode | `as_of_callers` 30d | `getDb` | 0 — *explicit no-data* | 16.6 ms |
| Opencode | `as_of_callees` now | `create` | 66 | 12.9 ms |
| Opencode | `what_changed` 7d | — | 0 | 13.0 ms |
| Opencode | `as_of_impact` | `getDb` | 136 | 29.9 ms |
| ortho | `as_of_callers` now | `str` | **355** | 15.1 ms |
| ortho | `as_of_callers` 30d | `str` | 0 — *explicit no-data* | 22.0 ms |
| ortho | `as_of_callees` now | `dict` | 147 | 16.9 ms |
| ortho | `what_changed` 7d | — | **5,609** | 49.5 ms |
| ortho | `as_of_impact` | `str` | 726 | 55.6 ms |

**Caller counts now vs 30 days ago:** every repo returned 0 at 30d with an
explicit reason — *"requested time … predates the graph's earliest record"*.
This is the designed no-data contract, not a silent fallback to current state.
It also means **the before/after comparison the section asked for could not be
made**: each graph has one sync, so there is no 30-day history to compare
against. Real temporal deltas require a repo synced repeatedly over time.

**Why ortho's `what_changed` differs:** its last commit is 2026-08-08 (5 days
before the run), inside the 7-day window, so all 5,609 facts register as added.
The other three repos' last commits are 8–29 days old, so their windows are
correctly empty. `valid_at` is git commit time, not wall clock — by design, so
re-syncing an old checkout cannot claim to be current.

**Graph health:** all four report `stale`, correctly, for the same reason —
freshness is measured against commit time, and none of these repos was committed
today. Orphans: 0 everywhere; no gc warning.

---

## Wedge 3 Results

Every repo ran the full two-agent scenario with 5 real `qualified_name`s pulled
from its own graph. All passed.

| Repo | Lock latency (avg) | Conflict detected | Conflict latency | Provenance query | Events |
|---|---|---|---|---|---|
| Setu | 0.69 ms | ✅ held_by `agent-A` | 0.89 ms | 0.12 ms | 4 |
| MediAssist | 0.80 ms | ✅ held_by `agent-A` | 0.11 ms | 0.12 ms | 6 |
| Opencode | 1.03 ms | ✅ held_by `agent-A` | 0.09 ms | 0.11 ms | 10 |
| ortho | 0.63 ms | ✅ held_by `agent-A` | 0.09 ms | 0.12 ms | 12 |

In every repo: agent-A locked nodes 1–3, agent-B's attempt on node 2 was refused
with the holder **and** their stated intent (`"refactoring auth flow"`), agent-A
logged provenance and released, agent-B then acquired node 2 successfully and
logged its own event. `who_touched(node 2)` returned a complete trail containing
**both** agents with timestamps and reasons on all four repos.

Final ledger state: 2 active locks, 14 events.

---

## Wedge 4 Results (Opencode + ortho)

> **The LLM was stubbed.** No API key is available in this environment, so rule
> generation and detectability snippets came from a deterministic stub.
> **ast-grep 0.45.1 and OPA 1.19.0 ran for real**, as did Kuzu and the ledger.
> Everything below except the LLM text is a live result.

| Repo | Candidate | Callers | Automatable | Detectability | FP risk | Files | Blocks | Warns | Passes |
|---|---|---|---|---|---|---|---|---|---|
| Opencode | `getDb` | 68 | ✅ | ✅ passed | No | 3 | 0 | 0 | 3 |
| ortho | `str` | 355 | ✅ | ✅ passed | No | 3 | 0 | **64** | 0 |

Rule generation: 0.8–0.9 ms (stubbed). Detectability: 176–226 ms, **real
ast-grep** on both the positive and negative snippets.

**Promotion changed nothing.** Both rules were promoted to `blocking`
successfully (`status: blocking` confirmed in the store), and re-running
enforcement produced **identical verdicts**. This is the temporal interlock
working: ast-grep matched `str(...)` 64 times in ortho, but `graph_confirmed =
False` for every match, so OPA returned `warn` per its third branch — *"pattern
matched but graph does not confirm deprecation"*. A pattern match alone never
blocks.

### Block path — proven separately

Because the above leaves the block path unexercised, a dedicated test forced a
real supersession: ortho was synced, then re-synced with all 355 edges touching
`str` removed, genuinely closing those facts (`invalid_at` set).

| Check | Result |
|---|---|
| Edges superseded | 355 |
| `deprecation()` says deprecated | ✅ True, since `2026-08-13T12:05:02` |
| Verdict on a file containing `str(1)` | ✅ **block** |
| Provenance stamped | ✅ event id 17 |
| Event reason | `rule blocktest2-deprecated: pattern matches deprecated node confirmed by temporal graph` |
| Event attribution | `node_id=str`, `agent_id=validation-run`, `session_id=val-block` |

**`chronos_rule_report(since_days=1)` returned `blocks: 0` and an empty
`top_violated_rules`** during the main run, correctly — there were no blocks to
report. `warns` and `passes` are `null` by design (only blocks are persisted).

---

## Performance Summary

| Metric | Min | Avg | Max |
|---|---|---|---|
| Temporal query latency | 7.2 ms | 20.2 ms | 55.6 ms |
| Lock operation latency | 0.44 ms | ~0.8 ms | 2.12 ms |
| Conflict detection | 0.09 ms | 0.30 ms | 0.89 ms |
| Provenance query | 0.11 ms | 0.12 ms | 0.12 ms |
| Enforcement per file (Opencode, no trigger) | 96.9 ms | 125.9 ms | 174.9 ms |
| Enforcement per file (ortho, 64 matches) | 196.4 ms | 4,872.7 ms | 5,285.2 ms |

- **Index throughput:** 108–711 nodes/sec, rising with repo size (fixed startup
  cost amortizes). Largest repo indexed in 15.3s.
- **Sync throughput:** 333–658 facts/sec.
- **Query latency stays sub-100ms** at every size tested, including ortho's
  726-node transitive impact query at 55.6 ms. Well inside the sub-second p95
  target.

### Cross-wedge trigger overhead — the significant finding

| Condition | Latency |
|---|---|
| Blocking enforcement, `CHRONOS_AUTO_TRIGGERS=true` | **5,099.5 ms** |
| Identical call, `CHRONOS_AUTO_TRIGGERS=false` | **134.4 ms** |

**A 38x increase, ~4.9 seconds added per blocking verdict.** Trigger 1 calls the
Reflector synchronously inside `enforce()`, so the LLM round-trip (here, a
failing call retried by litellm) lands directly in enforcement latency. On a PR
touching many files with several blocks, this compounds linearly. The verdict is
never *wrong* — isolation holds, the block still stands and is stamped — but the
CI job gets slower in proportion to how much it blocks.

ortho's 4,872 ms average per file in the table above is the same effect: 64
matches, each invoking OPA and the Wedge 1 lookup.

---

## Cross-Wedge Trigger Verification

| Trigger | Fired | Evidence |
|---|---|---|
| **1 — CI block → Reflector** | ✅ | `auto-trigger: block on r -> reflector returned None (low-signal)` in Section 4; in the block-path test it invoked the Reflector, whose LLM call failed with no API key and was **swallowed without affecting the block** — verdict and provenance stamp both intact. |
| **2 — Deprecation → coverage check** | ✅ | `auto-trigger: node str deprecated — covered by rule blocktest-deprecated`, emitted from `sync.py` during the forced supersession. |
| **3 — Lock conflict → coordination hint** | ⚠️ Not observed | Conflicts were detected correctly on all four repos, but the trigger's INFO log was not captured because the Section 2 harness did not attach a log handler. The code path is unit-tested in `test_unification.py`; **it was not independently confirmed in this run.** |

**Kill switch verified:** with `CHRONOS_AUTO_TRIGGERS=false`, `enabled()` returns
`False` and all three triggers made **0** Reflector calls; flipping to `true`
produced exactly 1. Confirmed by spy.

---

## Unified Server Verification

- **19/19 tools registered** on a single FastMCP server named `chronos`.
- **All four deprecated aliases warn correctly** on stderr and start the unified
  server: `chronos-graph-mcp`, `chronos-ledger-mcp`, `chronos-playbook-mcp`,
  `chronos-enforce-mcp`.
- **`chronos doctor` full output:**

```
vendored src: present
indexer     : ...\vendor\codebase-memory-mcp\build\c\codebase-memory-mcp.exe
upstream db : ...\.cache\codebase-memory-mcp\...-ortho.db
schema      : nodes=nodes{'id':'id','name':'name'} edges=edges{'src':'source_id','dst':'target_id','type':'type'}
upstream    : 10889 nodes, 5678 temporal edges
chronos     : empty | 0 nodes | 0/0 facts current | last None
database    : ...\validation.db | 2 locks | 14 events | 1 rules | 0.0MB
packmind    : not configured (...)
ast-grep    : ok | ast-grep 0.45.1
opa         : ok | v1.19.0
enforce     : ok | 1 blocking, 0 warn-only
```

Note `chronos : empty` — doctor reads the default group, while this run wrote
per-repo groups (`val_<repo>.kz`). Not a fault, but doctor gives no way to
inspect a named group, which a partner running multiple repos will hit.

---

## Failures & Gaps

**No section failed. `failures[]` is empty.** The items below are real gaps found
by the run.

1. **Node records carry no language field at all.** Verified directly: an
   indexed node dict has exactly four keys — `kind`, `name`, `path`, `qname`.
   There is no `language` key, so all 24,713 nodes across all four repos report
   `'?'`. Wedge 4 filters active rules by language
   (`get_active_rules(language=...)`), so a rule registered as `typescript`
   cannot be selected by a language derived from the graph. The validation
   worked around this by passing the language explicitly. **Per-language rule
   scoping is non-functional in any flow that derives language from a node** —
   the fix is either to populate the field during indexing or to derive language
   from the file extension in `path`.

2. **Trigger 1 adds ~4.9s per blocking verdict** (38x). Synchronous LLM call in
   the enforcement path. Needs to be made async/queued before a partner runs
   this in CI with blocking rules enabled.

3. **Zero blocks on real repo files.** Correct behavior, but worth stating: with
   a single-sync graph nothing is deprecated, so promoted rules still only warn.
   A partner will see no enforcement until their graph has history. The block
   path itself is proven working (above).

4. **The 30-day temporal comparison could not be made.** Every graph has one
   sync, so `as_of_callers(…, 30d)` predates all history on all four repos. The
   headline "what called foo before last week's refactor" capability is
   *structurally* verified (supersession works, as-of queries filter correctly)
   but has **never been demonstrated on a repo with real accumulated history**.

5. **Trigger 3 not independently observed** in this run (see above).

6. **LLM stubbed throughout.** No API key present. Rule generation and
   detectability snippet generation are unexercised live; `rule_generator`'s
   prompt has never produced a real ast-grep pattern from a real model.

7. **Packmind unconfigured** — Wedge 2 remains entirely mock-verified, unchanged
   by this run. `chronos_capture_lesson` and `chronos_query_playbook` were not
   exercised.

8. **`chronos doctor` cannot inspect a named group**, reporting `empty` while
   four populated graphs existed.

9. **Spec deviation:** `python -m chronos index <repo_path>` (as written in the
   task) is not a valid invocation — the CLI takes `--repo`. The harness called
   the Python API directly for precise timing.

---

## Partner Readiness Assessment

**Verdict: ready for a design-partner demo of Wedges 1 and 3; not ready for a
partner to run Wedge 4 in blocking mode, and not ready to show Wedge 2 at all.**

**What is genuinely solid.** The temporal graph is fast, correct, and handles
real third-party code without a single indexing failure — 24,713 nodes across
four repos, sub-100ms queries at every size, zero orphans. The intent ledger is
excellent: sub-millisecond locks, correct conflict attribution with holder and
intent, complete provenance trails. Both would demo well today, on a partner's
own repo, live.

**Blockers before a partner runs enforcement in CI:**

- Trigger 1's 4.9s-per-block latency must move off the enforcement path.
- Language population must be fixed, or per-language rule scoping documented as
  non-functional.

**Acceptable known gaps** (disclose, don't fix first): Wedge 2's Packmind
integration is mock-only and should simply not be shown; the LLM-generated rule
path needs one live run with a key; enforcement will be silent until a graph
accumulates history, which is a property of the design worth explaining rather
than hiding.

**The honest framing for a partner conversation:** Chronos's temporal memory and
agent coordination layers are real and measurably fast. Enforcement is built and
its safety interlock demonstrably works — it refuses to block without temporal
proof — but on a fresh install it will warn, not block, until history exists.
The self-improving playbook is written but unproven against a live Packmind.
