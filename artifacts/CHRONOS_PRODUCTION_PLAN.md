# Chronos Production Plan

*Drafted 2026-11-03. Covers taking Chronos from a working prototype to something real developers can install and trust, on any OS, with any agent.*

## Where we are

**Works today (verified):**
- The core engine: indexer, resolver, firewall, locks, memory and audit. Full suite: 161 passed (3 Nov, after the resolver fix).
- Claude Code integration through hooks and MCP. Verified live with haiku on a clone of CNG, on Linux only.
- Six bug fixes plus the resolver ranking fix, with regression tests. All uncommitted on `tanish`.
- Architecture diagrams: https://claude.ai/artifact/3Y7oTGAyUgjhLNsWK6yFQP

**Known gaps:**
- `Bash` is not governed (verified bypass). `Grep` and `Glob` can also expose secrets.
- Hooks fail open, so a missing or broken `chronos` silently disables enforcement.
- Install pulls about 2 GB because OpenHands is a hard dependency.
- Hook commands hardcode the venv interpreter path.
- Big repos: the first prompt does a full index inside a 30 s hook timeout.
- Gate patterns like `*auth*` produce false positives (`author.py`).
- Injected context comes from repo text, so it's a prompt-injection channel.
- The audit log lives in the repo, where the agent can rewrite it.
- The README benchmark numbers were measured with the inverted resolver ranking, and with verification that already passed on the untouched code (so "success" meant "didn't break tests").
- `AgyLLM` runs the `agy` CLI with `--dangerously-skip-permissions` as a fake model: the inner agent is ungoverned, context is cut to 6 messages, and tool calls are parsed from text. Fine for local benchmarking, not for the product.

**Decided (2026-11-03):**
- **First customers are individual developers.** Optimize for a one-command install, low friction, few false positives, and usefulness from day one. Team and enterprise features come later.
- **Source code never leaves the machine.** All indexing, resolution, policy and memory run locally. Chronos must be fully useful with no account and no network.
- **Don't build a competing harness.** Developers keep their own agent (and subscription). Chronos governs it from outside with three layers: hooks (context, clear messages), an OS sandbox (real enforcement, including shell), and an orchestrator (`chronos run --engine ...`) that checks the work. OpenHands stays as the headless/CI engine and benchmark runner.
- **Benchmarks run on Gemini through OpenHands;** Claude is used only for 1–2 small haiku checks.

**Still open:**
1. Buy or build the graph (codebase-memory-mcp) and the firewall (Cupcake). Phase 1 decides.
2. Install into the user's real `~/.claude` (user scope): waiting for the user's go-ahead.
3. Windows support for the sandbox layer.

## Phase 0: Stabilize what exists (1–2 days)

| Task | Done when |
|---|---|
| Re-run the full suite after the resolver fix | All green, or failures explained |
| Collect the running Gemini benchmark (CNG tasks 01, 02, 04, 05) | **Done (Flash Low, 1 run/task, hidden acceptance tests):** success 2/4 vs 2/4; tokens 651k → 345k (−47%); wall time 645 s → 418 s (−35%). Task 04 failed in both arms (both hit the 20-step cap without editing). n=1 per task through the `agy` wrapper: directional only. Medium/High need much longer timeouts (one Medium call measured 181 s, 24.5k thinking tokens). |
| Review the diff with the user, then commit to `tanish` | One reviewed commit per logical fix |
| Hidden acceptance tests per task (fail on baseline, pass on a reference fix) | **CNG done (4 tasks).** Chronos, Cooling and MediAssist still to do |
| Benchmark harness reliability | **Done:** per-repo `test_venv`, all runs saved incl. errors, `--timeout` overrides suite tasks, configurable `CHRONOS_AGY_TIMEOUT`, fixed `.venv/bin/` path rewrite |
| Re-measure the README benchmark claims with the fixed resolver | README numbers updated or caveated |

## Phase 1: Buy-vs-build spike (3–4 days, near-zero Claude tokens)

**1a. Firewall replay (no LLM).**
- Record about 30 real Claude Code hook events covering: edit a protected file, read `.env`, `sed -i` through Bash, `Grep` on secrets, normal edits, paths outside the repo, and malformed input.
- Replay the same events through Chronos and through Cupcake.
- Compare: decisions, p50/p95 latency, Windows install, recent commit activity, and whether Cupcake's signals can express Chronos's gate approvals and locks.

**1b. Graph arm in the Gemini benchmark.**
- Attach codebase-memory-mcp to the OpenHands runner over MCP. First confirm OpenHands can load an MCP server in this setup.
- Run the same four CNG tasks.

**Decision rules (agreed before seeing the results):**
- **Graph:** if codebase-memory-mcp is within 10% of Chronos on tokens and has equal success, adopt it and retire Chronos's indexer. Otherwise keep ours.
- **Firewall:** adopt Cupcake only if it (a) installs on Windows, (b) has commits in the last 60 days, and (c) can consume Chronos's locks and approvals. Otherwise keep Chronos's engine.

**Deliverable:** `artifacts/BUY_VS_BUILD_DECISION.md`.

## Spikes (cheap, run when convenient)

1. **OS sandbox: done (3 Nov), it works with one important rule.** Claude Code (haiku, bypassPermissions) under bubblewrap with `auth.py` read-only: the `sed -i` that got through before failed (`Device or resource busy`), `auth.py` was unchanged, and a normal append to `config.py` still worked ($0.005). A no-model attack matrix then found that **protecting only the file path is not enough**: renaming its parent directory moved the real file away and let `git checkout` write a new one at the protected path. Binding every ancestor directory up to the repo root onto itself blocks that (mount points cannot be renamed): append, overwrite, truncate, rm, mv, cp, Python writes, tee, hardlinks, `sed -i`, chmod and renaming the parent, grandparent or repo root were all blocked. Not covered: destructive changes to *unprotected* siblings (e.g. `rm -rf` of the directory still removes them).
2. **`chronos run --engine claude`:** resolve context, start the vendor CLI in the sandbox, enforce with hooks, run tests when it stops, resume with the failure output (max 2 rounds, haiku).

**Benchmark caveat:** the Gemini runs go through `AgyLLM`, which sends only the last 6 messages, parses tool calls from text, and restarts `agy` each turn. Relative comparisons (Chronos vs vanilla) are fair; absolute numbers say little about real Claude Code or Cursor use. Flash High exceeds 180 s per call on the OpenHands prompt, so use Medium or raise `CHRONOS_AGY_TIMEOUT`.

## Phase 2: Packaging (about 1 week)

| Task | Detail |
|---|---|
| Split dependencies | **Done.** Core install is 73 MB (was 2.0 GB) with no OpenHands/LiteLLM. Extras: `[agent]`, `[bench]`, `[kuzu]`, `[all]`. Verified in a clean venv: doctor, sync, resolve, firewall hook (0.29 s), context hook, MCP (16 tools + live call). |
| Lazy imports everywhere | **Done.** Firewall and session moved into `chronos` (`chronos.governance.firewall`, `chronos.context.session`); `runtime.*` kept as aliases. `tests/test_packaging.py` imports every core entry point with OpenHands blocked. |
| `chronos doctor` | **Done.** OpenHands reported as optional with its real version; `chronos run` and `chronos-bench` print which extra to install. |
| Stable hook command | **Done.** Hooks call the install's own `chronos` launcher by absolute path (survives upgrades), legacy interpreter hooks are migrated on reinstall, and `chronos claude status` runs each installed hook command as a probe. |
| Publish | **Name decided: `chronos-agent`** (`chronos` on PyPI is an unrelated ncurses stopwatch; import name and CLI stay `chronos`). Not published yet; until then: `uv tool install /path/to/chronos` (verified, 75 MB). Also rename the top-level `runtime`/`benchmarks` packages before publishing, so they can't collide with other packages. |
| Claude Code plugin | Bundle hooks and MCP in one plugin. Verify the current plugin manifest format first. |
| Package managers | Homebrew, Scoop and winget wrappers. |
| Frozen binary | Deferred. If built later, it must be a folder bundle, because hooks start a new process per call. |

**Done when:** a fresh Linux, macOS and Windows VM goes from nothing to a working hook in two commands, with a core install under 100 MB.

## Phase 3: Reliability harness (about 1 week, in parallel with Phase 2)

1. **Contract tests.** Replay the recorded hook events in CI against at least two Claude Code versions.
2. **Cross-OS CI matrix** on Linux, macOS and Windows. **Written** (`.github/workflows/ci.yml`): core job tests the built wheel without OpenHands on 3 OSes × Python 3.12/3.13 and runs the live canary; full job runs everything on Linux. Simulated locally on Linux (180 passed, 7 skipped). macOS/Windows not yet run.
3. **Latency budgets.** Pre-tool hook p95 under 150 ms; prompt hook p95 under 1 s on a 5k-file repo. **Measured:** pre-tool hook went from 558 ms to 387 ms median (dev env) and about 210 ms (core install) after lazy imports and a CLI fast path. The remaining ~190 ms is pydantic import, so 150 ms needs the Phase 5 daemon or pydantic-free hook models.
4. **Chaos tests.** Concurrent hooks against one DB, process killed mid-index, corrupted DB, missing binary, read-only `.chronos/`. **Concurrency done:** 32 parallel hook processes broke the audit hash chain (real bug, fixed with a cross-process file lock). Also done: missing binary (live canary), corrupted DB (asks, context fails open), unwritable audit log (asks instead of locking out every edit; was a hard deny), process killed mid-index (verified to land mid-index; next call recovers with an intact DB).
5. **Release smoke test.** One haiku `claude -p` run per release, costing cents.
6. **`chronos doctor --live`.** Sends a canary event through the real hook path and checks the decision. **Done:** protected edit held, secret denied, normal edit allowed; runs in CI.

**Done when:** all of the above run in CI, and a regression fails the build.

## Phase 4: Close the security gaps (1–2 weeks)

| Gap | Fix |
|---|---|
| `Bash` bypass | **Primary (spike passed on Linux):** `chronos exec -- <agent>` runs the agent in an OS sandbox built from policy: protected files read-only **and every ancestor directory bound onto itself** (required: see the spike), secrets hidden, network optional. Linux: bubblewrap/Landlock. macOS: `sandbox-exec`. Windows: open problem (WSL or containers first). Where the agent has its own sandbox mode, generate its config from Chronos policy instead. **Fallback:** shell-command parsing that denies writes to protected paths (redirects, `sed -i`, `tee`, `mv`, `cp`, `rm`); best-effort only. |
| Backstop | A git pre-commit hook and a CI/PR check that re-run the policy on the diff. This catches everything the hooks miss. |
| `Grep`/`Glob` secrets | **Done for Grep:** denied when its `path` or `glob` targets secrets. Repo-wide Grep relies on ripgrep skipping `.gitignore`d files such as `.env`. Glob only lists names, so it is left alone. |
| Fail-open | **Done:** if the firewall hook fails it now *asks* (visible, not a lockout); `policy.on_hook_error` can be `deny` or `allow`. Context injection still fails open. `chronos claude status` probes every installed hook. |
| Gate false positives | **Done:** default patterns match whole words (`author.py`, `AUTHORS` no longer gated); `policy.protected_modules` defaults to `ask`; secrets, path escapes and lock conflicts stay hard denies. The MCP `chronos_check_gate` tool now uses the same rules (it used to ignore `protected.yml`). |
| Prompt injection | **Done:** injected context is labelled untrusted, wrapped in a frame it cannot close, and capped (`policy.max_context_chars`). |

**Done when:** the replay set from Phase 1 has zero bypasses, and the security tests pass on all three OSes.

## Phase 4b: Orchestrator, `chronos run --engine` (1–2 weeks, after the spike)

Chronos drives the developer's own agent CLI as a worker instead of disguising it as a model.

1. Resolve context locally and inject it through the hooks.
2. Start the engine (`claude -p`, `agy`, `codex`, or OpenHands for API keys/CI) inside the sandbox.
3. Hooks enforce file policy and audit every decision while it works.
4. When it stops, Chronos runs the task's tests itself.
5. If they fail, resume the same session with the failure output, up to N rounds.
6. Distill memories from the transcript.

**What it adds:** workflow enforcement ("not done until tests pass") without owning the model loop, using the developer's own subscription with native tool calling. **Limit:** control is per session, not per turn. Each engine needs a small adapter for hooks and session resume.

## Phase 5: Daemon (about 2 weeks; only if Phase 3 shows the need)

- **`chronosd`:** one per machine, with a file watcher, background incremental indexing, and a socket the thin hook client talks to.
- This fixes big-repo timeouts, per-call startup cost and SQLite contention.
- The hook client falls back to in-process evaluation if the daemon is down.

**Trigger:** Phase 3 shows the latency budgets can't be met in-process, or the chaos tests show contention.

## Phase 6: Local-first product polish for individual devs (2–3 weeks)

With individual developers first and no code leaving the machine, the "control plane" becomes a local feature, not a cloud service.

1. **Local dashboard as the main UI.** `chronos ui` already exists. Make it show what Chronos did for you: denials, injected context, memories, token savings per session.
2. **Policy presets.** Short, readable presets (`relaxed`, `standard`, `strict`) instead of hand-written YAML. `standard` uses "ask" for protected modules.
3. **Memory that earns its place.** Show recalled memories in the dashboard, and let the developer edit or delete them.
4. **Local tamper evidence.** Keep the audit log outside the repo (for example in the user data directory) so the agent's file tools can't rewrite it. Server-side anchoring is not needed for this customer.
5. **Zero-config onboarding.** `chronos claude install` offers to enable the current repo and runs the first index in the background.

## Phase 6b: Optional cloud, later (only for paid or team use)

Strictly opt-in, and **metadata only**: never source code, file contents, docstrings, prompts or injected context.

- **Allowed to leave the machine:** aggregate counts, timings, decision types (allow/deny/ask), hashed repo IDs, and the user's own policy files if they choose to sync them.
- **Possible features:** settings and policy sync across the developer's machines, anonymous usage telemetry (opt-in), and later team policy sharing.
- **Enforcement in code:** one serializer whitelists the fields that may be sent, with a test that fails if a content field appears in any outbound payload.

Org-wide policy distribution, distributed leases, server-anchored audit and multi-tenant dashboards move to a future "teams" phase, after individual developers are happy.

## Phase 7: More agents (ongoing)

- Order: Cursor, then Codex CLI and Gemini CLI, then Windsurf.
- Each new host needs contract tests (Phase 3) before it ships.
- Hosts without hooks get MCP plus the git/CI backstop only.
- Use Cupcake's adapters if Phase 1 adopts it.

## Rollout

1. **Dogfood:** your own repos in advisory mode (log decisions, don't deny).
2. **Early users:** 10–20 individual developers via PyPI and the Claude Code plugin, with a feedback channel.
3. **Defaults:** the `standard` preset (secrets denied, protected modules ask) once false positives are below an agreed rate.

## Rough timeline

| Weeks | Work |
|---|---|
| 1 | Phase 0 and Phase 1 |
| 2–3 | Phases 2 and 3 in parallel |
| 4–5 | Phase 4 |
| 5–6 | Phase 4b (orchestrator) |
| 6–7 | Phase 5, only if triggered (overlaps Phase 6) |
| 6–8 | Phase 6 (local polish) |
| 9+ | Phase 7 (more agents), Phase 6b only if needed |
