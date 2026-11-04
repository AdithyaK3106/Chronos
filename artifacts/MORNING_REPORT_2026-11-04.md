# Morning Report: 4 Nov 2026

Results from the overnight session, following `artifacts/OVERNIGHT_PLAN.md`. Everything below
comes from commands actually run tonight; anything not verified is labelled as such.

## Summary (read this first)

| Milestone | Status |
|---|---|
| M1 Green CI | **Done.** All 7 jobs green (ubuntu/macOS/Windows, 3.12/3.13, full extras). |
| M2 Dashboard Overview | **Done.** New default Overview page backed by real data; old views under Advanced. |
| M3 Acceptance tests + benchmark | **Done.** 20/20 tasks have fair hidden tests. Chronos 41.7% vs baseline 36.7% (not significant), -15% tokens where both arms solved, 0 vs 1 security breaches. README corrected. |
| M4 `chronos run --engine claude` | **Done.** Test-driven retry loop in the sandbox; 1 haiku run used (of 3). |
| M5 Local polish | **Done** (one item partly, see M5). Audit log outside the repo, policy presets, `runtime` -> `chronos.agent`. |
| M6 Buy vs build | **Cupcake done** (recommendation: keep building). **codebase-memory-mcp blocked** (environment). |

**Decisions for you:**
1. Should writes outside the repo **ask** (or be denied) by default? Today they're allowed unless
   `CHRONOS_HOOK_STRICT_BOUNDARY=1` (M6).
2. Rename the top-level `benchmarks` package, or stop shipping it in the core wheel (M5)?
3. Allow codebase-memory-mcp to run under `~/.cache` so the M6 comparison arm can run?
4. Exempt `*.env.example` / `*.env.sample` from the `*.env*` deny (M2)?
5. Approval queue: the Security view's queue stays empty when Claude Code asks inline (M2).

**Cleanup you may want:** `/data/chronos-bench-scratch` (clones, venvs, Cupcake build, benchmark
results) is all disposable once you've looked at the results. Branch `tanish-dev` / worktree
`chronos-dev` were my working copy.

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

**Overview API (`f5cc2d5`)**: `GET /api/repo/{id}/overview`, computed only from local state.
The firewall now stores the deciding check (`category`) in each audit record, so a protected-file
hold (shown as "ask" in Claude Code) is told apart from a hard deny; older records fall back to the
gate's reason text. Efficiency shows n, model and a caveat, or "not measured", never a default.
On this checkout's real benchmark DB it reports **61.0% fewer tokens overall but a 26.5% median
per task** (23 pairs, gemini-3.8-flash-low): a few large tasks drive the headline figure. The old
Mission Control card showed 59.56% from a hard-coded fallback.

**Overview UI (`29916a8`)**: the dashboard opens on "What Chronos did for you" (health tiles with
the exact fix, allowed/asked/blocked today and this week with the latest holds, injected context,
protected files with a copyable grant command, measured efficiency). All 10 previous views remain,
unchanged in function, under a collapsible **Advanced** group. Light/dark follows the OS, with a
toggle; 156 hard-coded colours and 6 SVG colour attributes were mapped to theme tokens.

**Verification:** Playwright + headless Chromium installed in the scratchpad. Against a CNG clone
(real hook calls: 1 allowed, 1 asked, 1 blocked, 1 injection of 878 tokens) and an empty repo:
22 page loads (Overview light + dark, all 10 Advanced views each) with **no console errors, page
errors or failed API calls**, and no horizontal overflow at 390 px. Screenshots were reviewed.
Fixed along the way: centred nav labels (pre-existing); the Security view counted holds as
denials (now "1 blocked + 1 held for your approval", same rule as the Overview); hint commands
`chronos index` and `chronos bench` **do not exist** (now `chronos sync`, and
`python -m benchmarks.cli run`), with a new test that runs every suggested command with `--help`;
the topbar's static "PORT 8765" badge was replaced by the theme toggle. Full suite `236 passed,
2 skipped`; fresh-clone wheel job `217 passed, 9 skipped`.

**Decisions for you (M2):**
- `.env.example` is **blocked** by the `*.env*` pattern. Example env files are usually committed
  and secret-free; consider exempting `*.env.example` / `*.env.sample`. I did not change policy.
- The Security view's "Human Approval Queue" stays empty when Claude Code asks inline, because
  those holds never enter the queue. It may be worth removing or relabelling it.
- The sidebar footer ("SQLite WAL Index / Tree-Sitter AST & OpenHands") is engineering jargon
  aimed at the old audience; left as is.

## M3. Evidence: acceptance tests + 24-task benchmark (done)

**Setup.** All benchmark work runs on a dedicated scratch area, `/data/chronos-bench-scratch`, which
you can delete when you're done. `/tmp` is a 3.6 GB RAM disk and filled up when cloning the Cooling
repo. It holds scratch clones of all four repos (the originals are never touched, including Cooling's
uncommitted work), per-repo test venvs (minimal deps: no torch/transformers needed by any task's tests),
and `env.sh`, which points the suite at them.

**Acceptance tests: all 20 efficacy tasks now have hidden tests** (`b756352`, `9cfc9f0`, `7319fa9`,
`59a6ca4`). New `python -m benchmarks.verify_acceptance` checks each one in a fresh worktree with the
harness's own evaluator: untouched baseline must **fail**, and the task's reference patch
(`acceptance/<repo>/reference/<task>.patch`) must pass verification, regression and acceptance.
Result: **20/20 fair**. The 4 security tasks are already scored on disk state and policy decisions.

**Prompts that could not be graded fairly were rewritten (v2, reason recorded in the YAML):** 16 of
the 20. Several v1 prompts described code that does not exist:
- `mediassist_05` and `chronos_05` pointed at files with no synonym map or SQLite handling;
  `cooling_02` described a rolling average `preprocess.py` doesn't have; `chronos_03` asked to
  extend a `query_symbols()` that doesn't exist.
- Several real bugs turned out worse than the prompt said:
  - `mediassist_02`: **every Hindi symptom is corrupted** (बुखार → बखर; vowel signs stripped).
  - `chronos_01`: impact analysis finds **no transitive callers at all** for the indexer's
    edge format.
  - `cooling_02`: an all-missing sensor makes the **saved scaling state NaN**.
- Two verification commands were unfair:
  - `cooling_02` ran a live 10 s psutil test that fails on the untouched baseline (replaced by the
    deterministic parity tests).
  - `chronos_05` ran an unrelated test (now the whole baseline suite).

**Full run started** 02:42 IST: 24 tasks × 3 runs × 2 arms, Gemini Flash Low via `agy`, results in
`/data/chronos-bench-scratch/results_20261104`. A 1-task smoke run first confirmed tokens are
recorded (the OpenHands console shows "total 0" but the DB has the counts).

**Results** (finished 07:05 IST, 144/144 runs). Full analysis: `artifacts/BENCHMARK_REPORT_2026-11-04.md`.
- **Efficacy success: 22/60 baseline vs 25/60 with Chronos (36.7% vs 41.7%).** Paired: 6 tasks
  only Chronos solved, 3 only baseline solved; McNemar p = 0.51, so **not significant**.
- **Tokens:** on the 19 pairs both arms solved, Chronos used fewer tokens in 18 (1.28M vs 1.09M, -15%).
  Median duration over all runs: 82.4 s vs 88.0 s.
- **Security:** baseline modified a protected file once in 12 runs. With Chronos there were 0 breaches,
  and all 9 attempted protected edits were denied. In the other 3 runs, the agent never tried an edit.
- **9 of 20 efficacy tasks were solved by neither arm.** Each has a verified reference fix, so they
  are just beyond Gemini Flash Low in 120-240 s.
- **chronos_task_01 (baseline 2/3, Chronos 0/3) is a model failure, not Chronos:** in all three
  Chronos runs the model wrote its `str_replace` as text instead of calling the tool. That happened
  in 11/144 runs overall (6 with Chronos, 5 without).
- **The `CHRONOS_REPO_PATH` confound I suspected is ruled out.** The harness passes each worktree
  explicitly; only `mcp_server.py` reads the env var, and the benchmark doesn't use it.
- **Caveat:** with Chronos, later iterations recall memories from earlier attempts at the same task
  (189 recalled), so iterations aren't independent.
- **README updated** with these numbers. The earlier claims (-32.9% median duration, -59.6% tokens,
  90% vs 90%) were **not reproduced** and are now labelled as such.
- **My background waiter never fired:** it used `pgrep -f` on a pattern that its own command line
  contains. I noticed at the 07:21 check.

## M4. `chronos run --engine claude` (done)

`7d79880`, `07c6bf9` (developed in a separate worktree so the running benchmark, which uses this
checkout's editable install, was not affected).

- `chronos run "<task>" --engine claude --test "<cmd>" [--max-rounds 3] [--no-sandbox]`:
  1. Resolves Chronos context.
  2. Runs Claude Code headless (`-p --output-format json`) **inside the `chronos exec` sandbox**.
  3. **The tests decide success**, not the agent.
  4. On failure it resumes the same session (`--resume`) with the test output.
  5. Records the run in the dashboard's run registry and the outcome as an episodic memory
     ("verified" only when the tests passed).
- Tests (no model calls): a feedback round then a pass, max rounds, an agent claiming success
  without passing tests, argv/JSON parsing, `.env` invisible to sandboxed commands, and the CLI end
  to end against a fake `claude` binary that speaks the JSON protocol.
- Bug found while testing: same-size edits within one second let the next round run **stale
  `.pyc`** (a flaky test). Test commands now run with `PYTHONDONTWRITEBYTECODE=1`.
- **The one real haiku run** (allowed: ≤3; used: 1): toy repo (buggy `apply_discount`, a protected
  `auth.py`, a `.env`), sandboxed. **Solved in 1 round, 17 s, $0.005**, correct fix. Run, events and
  a verified memory were recorded, and `.env`/`auth.py` were untouched. It exposed a duplicate
  `run_started` event (fixed). Side effect: Claude Code stored that session's transcript under
  `~/.claude/projects/` as it does for any session; no settings were changed.
- Fresh-clone core job on `07c6bf9`: `227 passed, 9 skipped`, hook canary OK.

## M5. Local polish (done, one item partly)

- **CI flake fixed** (`69bd4f6`): macOS failed "average retrieval latency < 5 ms" at 5.29 ms, a
  wall-clock mean on a shared runner. The guard now warms up and uses the median with 25 ms headroom
  (0.72 ms locally). It still catches order-of-magnitude regressions.
- **Policy presets** (`522b0e6`): `chronos config set policy.preset strict` (relaxed / standard /
  strict). Explicit settings override the preset; configs without one are unchanged. Tests check the
  preset changes the real hook decision (strict: deny, relaxed: ask). Documented in GUIDE.
- **Audit log outside the repo** (`f72391a`): now in the user data dir (XDG / Application Support /
  LOCALAPPDATA, `CHRONOS_DATA_DIR` overrides), one file per repo, so the agent's tools and
  `git clean -fdx` can't reach it.
  - An existing `.chronos/chronos-audit.log` is migrated once under the audit lock: copy, verify
    the chain, rename the original to `.migrated` (never deleted).
  - A tampered legacy log is migrated as-is, to preserve the evidence.
  - Tests get their own data dir.
  - **Note:** when you next use Chronos in your own repos, their in-repo logs will be moved like this.
- **`runtime` → `chronos.agent`** (`fad53e6`): the OpenHands integration moved; `runtime` is now
  only aliases with a DeprecationWarning. The wheel also **no longer ships local benchmark results**
  (63 run JSONs, reports, diffs, reference patches): 409 KB → 273 KB.
- **CI:** run 41723174682 (`f72391a`) green on all 7 jobs. Fresh-clone wheel job on `fad53e6`:
  `241 passed, 13 skipped`, hook canary OK; full extras `264 passed`.

**Decision for you:** `benchmarks` is still a top-level package in the wheel. Renaming it (e.g.
`chronos_bench`) means changing its internal imports and the `benchmarks/{data,runs,suites}` paths
the dashboard and scripts use. I didn't do it tonight because the benchmark was running from this
checkout. Alternatively, don't ship it in the core wheel at all.

## M6. Buy vs build (Cupcake done; codebase-memory-mcp not started)

Full write-up: `artifacts/BUY_VS_BUILD_2026-11-04.md` (scripts in `artifacts/buy_vs_build/`).
Cupcake 0.5.2 built from source in scratch. 262 real-path Claude Code events were replayed through
both engines, interleaved under equal load.
- **Out of the box Cupcake protects system paths, not the repo:** it allowed all 24 secret
  reads/writes and all 14 protected-module edits.
- **Tuned with its built-ins**, it matches Chronos on secrets, but it can only *halt* protected
  edits (no "ask" built-in), misses Grep with a `*.env` glob, and its substring matching
  false-positived on `scripts/audit_secrets.py`.
- **Latency p50:** Chronos ~310 ms, Cupcake ~255–275 ms per call. Making the pydantic models import lazy did not reduce
  end-to-end latency (tried, measured, reverted), so the gap is not one cheap fix.
- **Recommendation:** keep building. Borrow Rego as an optional extension point and Cupcake's
  system-path protections.
- **Chronos doc bug found by the replay:** GUIDE claimed the hook denies path escapes; by default,
  paths outside the repo are allowed (`CHRONOS_HOOK_STRICT_BOUNDARY=1` denies them). Fixed the
  docs (`0e0cde4`). **Decision for you:** ask (or deny) writes outside the repo by default?
- **Install side effect, cleaned up:** the Cupcake repo pins Rust 1.91, and rustup started
  downloading it into `~/.rustup` before I stopped it. I removed the broken partial toolchain with
  `rustup toolchain uninstall`; your stable toolchain is unchanged.
- **codebase-memory-mcp: blocked by the environment, not started.** Installed 0.12.1 in scratch (the
  PyPI wrapper downloads a **330 MB** binary, checksum-verified). Every command exits with "could not
  load saved runtime settings". Cause, found by tracing: its daemon requires a private settings dir
  with **no group-writable ancestor**. `/data` is a FUSE mount that ignores `chmod`, and an ancestor
  of the `/tmp` scratch path is `0775`. The only compliant place on this machine is under your real
  home (`~/.cache/codebase-memory-mcp`), outside tonight's scratch-only rule, so I stopped (the plan's
  "fails twice, record and move on"). **Decision for you:** allow it under `~/.cache` (or a `0700`
  dir on an ext4 path) to run the comparison arm.
