# P0 + P1 plan (4 Nov 2026)

Priorities agreed on 4 Nov. Each step is committed on `tanish` when it is done.

## P0: make current claims true
- [x] **1. Bash gap.**
  - [x] PreToolUse on `Bash`: parse the command, apply the same rules as the file tools (secrets
        denied, protected modules ask, writes outside the repo ask, locks).
  - [x] PostToolUse backstop on `Bash`: detect protected/secret files that changed anyway; audit
        them and tell the agent.
  - [x] Install/status/uninstall include the new matchers; live hook canary covers Bash.
  - [x] Tests (no model calls), GUIDE updated.
- [ ] **2. Cheap measurements.**
  - [x] A: tokens per solved task (all runs, failures included) in the harness report.
  - [x] B: retrieval-only benchmark: recall of the reference-patch files/symbols in what the
        hook would inject, for all 20 efficacy tasks. No LLM.
  - [x] Fix the retrieval problems B shows (e.g. abstract method instead of implementation).

## P1: proof and daily feel
- [x] **3. Hook latency** from ~310 ms toward <100 ms per call (measure first; likely a
      persistent helper process or a slimmer import path).
      Done: ~300 ms -> 90-110 ms on every path (Edit, protected edit, secret read, Bash pre/post),
      measured on this checkout. The work itself is ~10 ms; the rest was imports (pydantic via
      eager package inits and the permission model, yaml, urllib). Also fixed: the Bash backstop
      snapshot listed every file of an ignored .venv (70 s per call here).
- [ ] **4. Realistic security benchmark:** indirect prompt injection planted in repo files, and
      Bash-based attacks. Gemini Flash via OpenHands, both arms.
- [ ] **5. Large-repo check:** 50k+ symbols: first index, per-prompt freshness, hook latency.

## Constraints
- Claude model calls: tiny only. Benchmarks use Gemini Flash via OpenHands (`agy`).
- Benchmark scratch: `/data/chronos-bench-scratch`. Never touch the original repos.
