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
- These fixes were pushed in the last commit of the night. **Its GitHub run was not checked**
  (usage limit reached). Linux tests pass locally (30/30 in the affected files).

## Status at stop
- M1: all fixes in; final Windows confirmation pending on GitHub. Not ticked.
- M2–M6: not started.
