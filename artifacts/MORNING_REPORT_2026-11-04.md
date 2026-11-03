# Morning Report: 4 Nov 2026

Results from the overnight session, following `artifacts/OVERNIGHT_PLAN.md`. Everything below
comes from commands actually run tonight; anything not verified is labelled as such.

## M1. Green CI

**Starting point.** The last GitHub run on `tanish` (run 41693067274, commit `6daec16`) failed
on all 7 jobs:
- macOS and Windows: `setup-uv` step, `ELOOP: too many symbolic links encountered, stat '.../guide.md'`
  (the `guide.md` → `GUIDE.md` link resolves to itself on case-insensitive filesystems). Already
  fixed locally in `b668259`, which had not been pushed.
- Ubuntu core and full: the test step. `gh` is not logged in on this machine, so the job logs
  could not be read; the public annotations API only gives "exit code 1". The fresh-clone
  reproduction below shows the cause.
- Node 20 deprecation is only a warning (GitHub forces Node 24), so `actions/checkout` and
  `setup-uv` versions were left alone, as the plan says.

**Fresh-clone reproduction** (`scratchpad/ci_repro`, exact `core` job: build wheel, install
`[test]` only, run pytest, live hook canary): `4 failed, 192 passed, 10 skipped`.
- `test_sync_benchmark_runs_into_registry` and `test_14_benchmarks_endpoint_*` read the
  git-ignored `benchmarks/data/benchmark_runs.db`.
- `test_16_zero_path_resolver_*` and `test_18_subgraph_and_git_endpoints` registered `Path.cwd()`
  and relied on the developer's own git-ignored `.chronos` index of the checkout, so on a fresh
  clone the repo was "Unindexed" (HTTP 400 / empty subgraph).

**Fixes**
- `bed414f`: tests build what they need instead of reading local state. `tests/conftest.py` now
  has `write_bench_db()` (synthetic `benchmark_runs.db` with the harness schema, 22 pairs, known
  60% savings) and a `ledger_git_repo` fixture (two-commit git repo with `acquire_lock` and a
  caller, indexed via `POST /index`). Three evaluation-studio tests that were being *skipped* on
  fresh clones now run too. `sample_repo` no longer symlinks the real `benchmarks/` (symlinks
  need developer mode on Windows).
- `2d45d3b`: on the second fresh-clone run a different test failed:
  `test_slm_resolver_graph_recursion_natural_words`. It and its sibling call a real model through
  the `agy` CLI (~30 s each, non-deterministic, `agy` absent on runners). They now run only with
  `CHRONOS_LIVE_LLM=1`. They passed 3/3 when run alone with the live model.

**Fresh-clone results after the fixes** (`2d45d3b`): core job (py3.13, wheel) `197 passed,
9 skipped`, exit 0 (the live hook canary was run on the `bed414f` clone: ask/deny/allow all OK,
~210 ms each); full job (all extras)
`216 passed, 2 skipped`, exit 0, but it took **21.5 min**.

**Product bug found via CI timing** (`ba20e17`): in the full job, three tests took ~410 s each
(vs ~30 s in core). The indexer pruned only directories literally named `.venv`/`venv`, so the
job's in-repo `.ci-venv` had its whole site-packages (OpenHands etc.) indexed. Real users with
a venv named `env`, `.venv-3.12`, etc. hit the same thing, including in the per-prompt freshness
walk of the Claude Code hook. Fix: `prune_ignored_dirs()` also drops any directory containing
`pyvenv.cfg`, plus `.tox`, `.nox`, `.eggs`, tool caches and `site-packages`. A regression test
fails without the fix and passes with it.
Fresh-clone full job on `ba20e17`: `217 passed, 2 skipped` in **65 s** (was 21.5 min).

**GitHub CI after the fixes:** run 41704303299 (`ba20e17`) and run 41704648704 (`139c0f5`):
Ubuntu core 3.12/3.13, Ubuntu full, and macOS core 3.12/3.13 (including the live hook canary) are
**green**. Windows 3.12/3.13 failed 4 tests each (read through the new JUnit→annotation step,
`139c0f5`, because job logs need auth):
- 3 × `test_claude_code_integration`: `is_chronos_hook()` did not match `"...\Scripts\chronos.exe"
  hook claude pre`. **Real Windows bug**: `status` showed no hooks, and re-running install would
  duplicate hooks that uninstall could not remove. Fixed (regex accepts `.exe`, case-insensitive)
  plus a regression test.
- `test_benchmark_acceptance::test_correct_change_passes_*`: the evaluator assumed `venv/bin` and
  `:` as the PATH separator. Now `Scripts` on Windows and `os.pathsep`.
- Run 41704965206 (`a866355`): **Windows 3.12/3.13 green**, Ubuntu and macOS 3.12 green.
  macOS 3.13 failed one test that had passed on the two previous runs:
  `test_concurrent_hooks_keep_the_audit_chain_intact`, `assert 27 >= 28`. Under 32 concurrent
  hook processes, one pre-tool decision was missing from the audit log (chain still valid).

**Audit gap** (`a944efb`): the firewall audits every decision it makes, including its own
exceptions, but the hook opens the DB (`get_connection()`: schema + WAL setup) *before* the
firewall runs. If that raises, `run_hook` falls back to `on_hook_error` (default: ask) and nothing
is audited. Now the hook writes a `tool_authorization` / `error` record before falling back. New
test: a DB-open failure gives `ask` plus exactly one audit record (fails without the fix). The
concurrency test now also asserts every ordinary edit got a silent allow and prints the hook
output if not.
**Not confirmed:** the exact exception on macOS. 22 local stress trials (incl. pinned to one CPU)
lost nothing, and the old test never captured hook output. If it recurs, the new assertion will
show it. Fresh-clone core job on `a944efb`: `200 passed, 9 skipped` in 50 s, hook canary OK.

**Result: M1 done.** GitHub run 41709575082 (`a944efb`): all 7 jobs green (Ubuntu core 3.12/3.13,
Ubuntu full, macOS 3.12/3.13 incl. live hook canary, Windows 3.12/3.13).

## M2. Dashboard: audit and data the Overview needs

**Audit** (every GET endpoint called against this checkout, which is indexed and has local
benchmark data, and against an empty repo). 10 pages: mission (current default), resolver, context,
runs, eval, graph, locks, memory, security, audit.
- **Bug, fixed (`dab9ece`):** `GET /memory` and `POST /memory/recall` returned **HTTP 500 as soon as
  any memory existed** (`MemoryRecord.to_dict` does not exist; they are pydantic models). Tests
  only covered empty stores. Regression test fails without the fix.
- `audit/verify` 404 on GET is expected (POST-only, and the UI uses POST).
- **Invented number:** `mission.stats.token_savings_pct` is 59.56 with a hard-coded source string
  ("23 paired tasks… 20 efficacy tasks"), and the code falls back to the literal `59.56` when the
  summary file is missing. The Overview must compute n and the model from the data or show nothing.
- Missing for the Overview: hook install status, sandbox availability, an allowed/asked/denied
  split (the audit log stores a protected-module "ask" as `deny`), and **any record of what the
  Claude Code hook injected**: only the dashboard's own Resolve button captured envelopes.

**Hook injections are now recorded (`bbcdcaf`):** each injection is stored as a context
envelope (mode `claude_code_hook`): files, symbols, top candidates, recalled memories, size. The
prompt is secret-redacted (tested with an `sk-live-…` key), recording is best effort, and prompts
that inject nothing record nothing.

**Major performance bug found on the way (`aac9a6a`):** adding that recording made
`test_killed_mid_index_recovers_cleanly` (22.5k symbols) time out. Cause: FTS maintenance was
**O(n²)**. Every `get_connection()` ran a backfill joining on the unindexed FTS `id` column, and
the sync triggers deleted FTS rows by `id` (a full scan per row). Measured before → after:

| Symbols | connect before | connect after | all-changed re-sync before | after |
|---|---|---|---|---|
| 1,500 | 0.50 s | 0.00 s | 0.69 s | n/m |
| 4,500 | 4.6 s | 0.00 s | 5.6 s | n/m |
| 22,500 | ~2 min (extrapolated; test timed out at 60 s) | 0.01 s | n/m | 1.06 s |

Hooks open the DB on every tool call with a 10 s timeout, so **on a repo above roughly 5–10k
symbols every hook call would have timed out**. Fix: FTS rows are keyed by the source rowid; a
one-time migration (`PRAGMA user_version` 2, under `BEGIN IMMEDIATE`) rebuilds existing DBs. Checked
on a *copy* of this checkout's real DB: migrated in 0.01 s, 0 mismatches, code search and memory
recall work. New tests: migration from the old format, plus a 4.5k-symbol timing test that fails on
the old code (connect 5.1 s). Fresh-clone core job on `bbcdcaf`: `205 passed, 9 skipped`, hook
canary OK.
