# Overnight Plan (4 Nov 2026)

Working checklist for the unattended night session. Each milestone ends with: tests pass →
commit → push to `tanish` → tick the box here → add a line to the morning report.

## Ground rules (agreed with the user)

- **Allowed:** commit and push to `tanish`; long Gemini (Flash Low, via OpenHands + `agy`) benchmark
  runs; up to **3 tiny haiku runs** in total; installing third-party tools in scratch directories.
- **Never:** delete anything important (user data, other repos, branches, history), push to
  `master`, force-push, publish to PyPI, or touch the real `~/.claude`.
- Other repos (CNG, Cooling project, MediAssist) are only read or cloned; benchmark work happens
  in copies/worktrees. `Cooling project` has uncommitted work: never modify it.
- Every claim in the morning report must come from something actually run tonight.
- If something fails twice, record it in the report and move to the next milestone.

## Milestones (in priority order)

### M1. Green CI (Linux, macOS, Windows)
- [x] Fix the 4 tests that fail only on a fresh clone (`test_sync_benchmark_runs_into_registry`,
      `test_14`/`test_16`/`test_18` in `test_ui_server.py`): they depend on local, git-ignored data
      or on the working directory being the Chronos repo.
- [x] Re-run the exact CI job on a fresh clone (`scratchpad/ci_repro`) until green.
- [x] Push; check the GitHub run. With `gh` logged in, read macOS/Windows logs and fix
      platform issues (msvcrt lock, hook quoting, path handling). Without it, use the public
      annotations API.
- [x] Update `actions/checkout` / `setup-uv` versions only if the Node 20 deprecation causes failures.
- **Status:** done. GitHub run 41709575082 (`a944efb`) green on all 7 jobs. Node 20 is only a
  warning, so action versions were not changed. See the morning report.
- **Done when:** the GitHub CI run for the pushed commit is green on all jobs, or remaining
  failures are explained in the report with the exact error.

### M2. Dashboard redesign: "What did Chronos do for me?"
Decisions: primary user is the individual developer; full redesign; **no build step** (one
self-contained HTML file served by `chronos ui`, same JSON API); light + dark following the OS with
a toggle; all current engineering views kept under **Advanced**.

- [x] Audit the current dashboard: list views, API endpoints used, broken/empty states.
- [ ] New **Overview** (default screen):
  - Health: hooks installed and working (`claude status` probe), sandbox available, index fresh, audit chain valid.
  - Today / this week: actions allowed, asked, denied (from the audit log), with the latest few
    decisions and their reasons.
  - Protected surface: which files are protected, which are approved (gate grants), one-click
    copy of the `chronos gate grant` command.
  - Context injected recently and its size; memories recalled.
  - Efficiency: benchmark token/time deltas with their caveats (n, model), never invented numbers.
- [ ] **Advanced**: runs and trace waterfall, context inspector, evaluation studio, graph, locks,
      audit records, memory: same functionality, consistent styling.
- [ ] Empty states that say what will appear and how to make it appear; errors that say what to do.
- [ ] New API endpoints only where the overview needs data that doesn't exist yet (with tests).
- [ ] Verify: server tests pass; load every view against a real indexed repo (CNG clone) and an
      empty repo; screenshot-free check via the API plus an HTML smoke test.
- **Done when:** `chronos ui` opens on the Overview, every Advanced view still loads, tests pass.

### M3. Evidence: acceptance tests + 24-task benchmark
- [ ] Test venvs for chronos, Cooling project, MediAssist (scratch dirs, like `cng_venv`).
- [ ] Hidden acceptance tests for the remaining efficacy tasks (fail on baseline, pass on a
      reference fix; fix or rewrite unfair prompts and record why).
- [ ] Full suite run: all repos, **3 runs per task**, Flash Low, in the background.
- [ ] Write `artifacts/BENCHMARK_REPORT_2026-11-04.md`: per-task table, totals, variance, caveats.
- [ ] Update README numbers or add a clear caveat.

### M4. Orchestrator: `chronos run --engine claude` (Phase 4b)
- [ ] Engine adapter interface; `claude` engine (headless `-p`, session resume); fake engine for tests.
- [ ] Loop: resolve context → start engine in `chronos exec` sandbox → run task tests → resume with
      failures (max N rounds) → record memories.
- [ ] Tests with the fake engine; at most one haiku run end to end.

### M5. Local polish (Phase 6), as time allows
- [ ] Audit log outside the repo (user data dir), with migration of the existing log.
- [ ] Policy presets `relaxed` / `standard` / `strict`.
- [ ] Rename top-level `runtime` / `benchmarks` packages (keep aliases) before any publish.

### M6. Buy-vs-build (Phase 1), only if everything above is done
- [ ] Cupcake: install in scratch, replay recorded hook events, compare decisions and latency.
- [ ] codebase-memory-mcp: install in scratch, add a benchmark arm.

## Morning report
Written continuously to `artifacts/MORNING_REPORT_2026-11-04.md`: what was done (commit hashes),
test and CI status, benchmark results, what failed and why, decisions that need the user.
