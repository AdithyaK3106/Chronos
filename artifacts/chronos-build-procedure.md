# Build procedure

Work through these steps in order, and finish each step's check before starting the next. New file paths are suggestions, and existing paths come from the current repo.

## Every week

Do these alongside the engineering work, because a working product without agreed pilots fails the plan.

- [ ] Hold at least 3 qualification calls, for 15 by December 4
- [ ] Ask each team for last month's agent spend, billing type, server access and whether vendor hosting would be acceptable
- [ ] Send one progress update to warm contacts
- [ ] Run the current tests and fix failures before starting new work
- [ ] Log what slipped, then use the cut order in the plan if you're behind

## Step 0: Set up before November 9

The goal is a clean starting point, with both target CLIs running and the two repos separated.

**Engineering**

- [ ] Create a `v1` branch in `chronos`
- [ ] Add GitHub Actions for `pytest`; the repo has no CI today
- [ ] Skip the two `tests/test_slm_resolver.py` tests when `agy` isn't installed
- [ ] Create a private `chronos-control-plane` repo with a commercial license
- [ ] Choose an available PyPI package name, since `chronos` is taken, and update `pyproject.toml`
- [ ] Install Claude Code and Codex CLI, and record the exact versions you'll support
- [ ] Read OpenAI's current hooks and managed configuration docs, and correct any Codex details in this plan
- [x] Write `docs/events-v1.md`, defining the shared format for tool calls, decisions, sessions and usage (47ddc35)

**Business**

- [ ] List 30 warm contacts and target companies
- [ ] Book the first 5 qualification calls
- [ ] Book a CA or company secretary to choose the entity type

**Check:** CI passes on `v1`, both CLIs run on a test repo, and 5 calls are booked.

## Step 1: Fix trust issues, November 9–15

The goal is that everything the public repo says is true and safe to try.

**Correct the public claims**

- [ ] In `README.md`, replace the 59.6% headline with the median, the spread and the benchmark's limits
- [ ] Remove "100%" and "Zero Breaches," and explain that the current security test isn't valid evidence
- [ ] Change "<15 ms" to say it covers only the resolver call
- [ ] Describe Chronos's enforcement as working only with OpenHands until the adapters ship
- [ ] Mark CodeScout SLM, Kuzu and the `agy` route as experimental and outside the commercial product

**Fix the installer**

- [x] Replace `chronos-agent/chronos` with `AdithyaK3106/chronos` in `install.sh`, `README.md` and `GUIDE.md` (97b3198; the repo is `AdithyaK3106/Chronos`, branch `master`)
- [x] Rewrite the MCP step in `install.sh` to back up and merge config files instead of overwriting them (3ff5f5d)
- [x] Add an installer test that confirms existing MCP servers survive (3ff5f5d)

**Fix the guardrails**

- [x] In `chronos/security/gates.py`, support the documented `policies` format, including `mode` and `denial_reason` (3665cae)
- [x] Reject unknown policy keys loudly instead of ignoring them (3665cae; `chronos doctor` shows the error)
- [x] Always deny agent writes to `.chronos/**`, `.claude/settings*.json` and `.codex/**` (0a9c3da)
- [ ] Replace broad patterns like `*auth*` with path rules such as `**/auth/**`, `.env` and `.env.*` (not done as written: defaults now match whole words in code/config paths and skip docs, 2b5c11f)
- [x] Add tests confirming `src/authors/list.py` and `docs/oauth.md` are no longer blocked (2b5c11f)
- [x] Add `chronos approve` and `chronos revoke` commands that use the existing grant functions and write audit events (b693aca)
- [ ] Make lock-check and audit-write failures in `runtime/hooks.py` deny protected actions instead of passing silently

**Prepare for the adapters**

- [ ] Move OpenHands to an optional `[openhands]` install, and stop importing it from guardrail and hook code
- [ ] Set memory to off by default in `chronos/core/config.py`
- [ ] Release the result as `v0.3.0`

**Business**

- [ ] Choose an entity type and start registration
- [ ] Draft the pilot agreement with the price, success targets and data terms

**Check:** A fresh install keeps existing MCP servers, the README policy blocks its target, and an agent can't edit Chronos's own policy or logs.

## Step 2: Build the policy engine, Claude adapter and token savings, November 16–22

The goal is one Claude Code session that blocks risky commands and gets useful context automatically.

**Policy engine**

- [ ] Create `chronos/policy/schema.py` for versioned rules covering tool, path, command and action (`allow`, `ask` or `deny`)
- [ ] Create `chronos/policy/shell.py`, using `tree-sitter-bash` to extract commands and redirect targets
- [ ] Handle chains, pipes, subshells and `bash -c`, and apply each policy's fallback to anything it can't parse
- [ ] Create `chronos/policy/engine.py` as a pure decision function with no OpenHands dependency
- [ ] Point `runtime/hooks.py` at the new engine so OpenHands uses the same rules
- [ ] Test `rm -rf src`, `cat .env`, `curl -d @.env`, `echo x > src/auth/login.py` and nested variants

**Claude Code adapter**

- [ ] Create `chronos/adapters/claude_code.py` to convert hook input into the shared tool-call format
- [ ] Return `permissionDecision` JSON, and a deny if anything inside the adapter crashes
- [ ] Add `chronos adapter install claude`, with project scope for testing and managed scope for pilots
- [ ] Generate `managed-settings.json` with `onFailure: block`, telemetry and optional `allowManagedHooksOnly` (dbf45fb: `chronos claude install --managed` with onFailure + allowManagedHooksOnly; telemetry not included yet)
- [ ] Require Claude Code v2.1.295 or later, and warn on untested versions (install warns below 2.1.295; it does not refuse)
- [x] Save hook payloads as test fixtures covering Bash, Edit, Write, Read and MCP calls (ac263c8, plus WebFetch)
- [ ] Run one live contract test against the real CLI before each release

**Token savings**

- [ ] Stop `runtime/session.py` from re-indexing the whole repo in each session
- [ ] Index only changed files using stored file hashes and the existing `update_file` function
- [ ] Fix re-sync on a large repo, testing against the 2,693-file repo size from the review
- [ ] Create a lightweight `chronos-hook` command that doesn't load heavy dependencies
- [ ] Measure it, and add a small local background service only if p95 stays above 500 ms
- [ ] Add a `UserPromptSubmit` handler that injects up to 1,500 tokens of files, symbols and tests
- [ ] Stay under Claude Code's 10,000-character hook limit so the context is actually seen
- [ ] Build a test set of 30 real prompts with file names removed, from 3 repos you didn't write
- [ ] Add a confidence score from resolver results, and inject nothing below the threshold

**Check:** A live Claude Code session blocks the test commands, receives context without an MCP call, and adds under 500 ms at p95.

## Step 3: Build the control plane and signed audit, November 23–29

The goal is a server that manages policies across devices and detects tampering with audit records.

**Foundation**

- [ ] Scaffold FastAPI, SQLAlchemy, Alembic and Postgres with Docker Compose in `chronos-control-plane`
- [ ] Build it for one customer per install, so v1 needs no multi-tenant code
- [ ] Create tables for admins, devices, policy versions, audit events, sessions, usage, PRs and alerts
- [ ] Add admin login with hashed passwords and secure sessions

**Device enrollment and policies**

- [ ] Add `chronos enroll --server <url> --token <token>` using one-time tokens
- [ ] Generate an Ed25519 key on each device and register only the public key
- [ ] Sign each policy version on the server
- [ ] Have devices poll every 60 seconds, verify the signature and cache the last valid policy
- [ ] Add `chronos policy status`, showing the version, last sync and any warnings
- [ ] Add policy editing and version history to a server-rendered dashboard

**Signed audit**

- [ ] Replace local-only logging in `chronos/audit/logger.py` with device-signed events containing sequence numbers
- [ ] Store events in a local SQLite outbox, and retry when the server is unreachable
- [ ] Verify the signature, sequence and chain on the server, then add a server signature
- [ ] Make the audit table append-only through database permissions and a trigger
- [ ] Flag missing sequence numbers and silent devices on the dashboard
- [ ] Add an audit verification command and export for customers
- [ ] Repeat the review's attacks: change `deny` to `allow`, delete recent events and replay old ones

**Check:** A policy change reaches an enrolled device within 5 minutes, the device keeps working offline, and all three audit attacks are flagged.

## Step 4: Add Codex, spend reporting and the waste report, November 30–December 6

The goal is one dashboard showing cost per merged PR and clear coverage for both agents.

**Codex adapter**

- [ ] Create `chronos/adapters/codex.py` using the shared tool-call format
- [ ] Return only `deny` for blocked calls, because `ask` and malformed output can let a call continue
- [ ] Add a catch-all so adapter crashes return a valid deny
- [ ] Generate `requirements.toml` with `[features] hooks = true`, `allow_managed_hooks_only` and `managed_dir`
- [ ] Provide scripts for `/etc/codex/requirements.toml`, and hook scripts for the managed directory
- [ ] Add context through `SessionStart` and `UserPromptSubmit`
- [ ] Test Bash, `unified_exec`, `apply_patch`, MCP and web paths, and record which are covered
- [ ] Show that coverage table on the dashboard and in the docs

**Telemetry and cost**

- [ ] Configure the OpenTelemetry collector to send usage data to the control plane
- [ ] Drop prompts, tool arguments and tool output in the collector, and filter `codex.sse_event`
- [ ] Set Claude Code telemetry through managed settings, leaving `OTEL_LOG_USER_PROMPTS` off
- [ ] Set Codex `[otel]` through managed config, and flag devices that stop reporting
- [ ] Normalize both agents into one usage format, keeping each token type separate
- [ ] Add a versioned model price table, and calculate Codex cost from tokens
- [ ] Label every figure as `estimated` or `calculated`

**Cost per merged PR**

- [ ] Send repo, branch and commit from `SessionStart` to the control plane
- [ ] Add a worker that polls GitHub or GitLab using a read-only token
- [ ] Add each branch's session cost to its merged PR, and show unmatched cost separately
- [ ] Build views by developer, repo, model and merged PR, with CSV export
- [ ] Add budget threshold alerts on the dashboard and through a webhook

**Waste and experiment**

- [ ] Add `PostToolUse` detection for repeated failing commands and repeated file reads
- [ ] After 3 failures in a row, warn the agent and log the event without blocking
- [ ] Add the waste report for repeated reads, failure loops, large outputs, cache use and model choice
- [ ] Add `report_only` mode for the baseline week
- [ ] Use a hash of repo and branch to keep 20% of branches as the comparison group

**Business**

- [ ] Finish the pilot agreement with a lawyer
- [ ] Get two pilots agreed by December 4

**Gate on December 4:** If two pilots aren't agreed, pause new engineering and focus on customer conversations and positioning.

**Check:** A test PR shows its calculated cost, the waste report ranks 5 sources, and comparison-group branches receive no injected context.

## Step 5: Package it and install the first pilot, December 7–13

The goal is an install a customer can trust and you can support alone.

**Server package**

- [ ] Pin all image versions in Docker Compose for the API, worker, Postgres, collector and HTTPS proxy
- [ ] Support customer-provided TLS certificates and an internal-network setup
- [ ] Write `install.sh`, `backup.sh`, `restore.sh` and `upgrade.sh`
- [ ] Run Alembic migrations automatically during upgrades, after taking a backup
- [ ] Add a health page for the database, collector, license, last device sync and disk space

**License**

- [ ] Define a signed license file with customer, seat count, expiry and features
- [ ] Verify it offline, with no calls to your servers
- [ ] Keep the signing tool and private key outside the product repos
- [ ] Warn admins when seats or dates are exceeded, without turning off enforcement

**Device rollout**

- [ ] Create a rollout kit with enrollment, managed settings, Codex requirements and an uninstall script
- [ ] Support manual rollout for 5 developers and include MDM instructions for larger rollouts

**Prove the install**

- [ ] Write the install guide, then follow only that guide on a clean Linux VM
- [ ] Time it, and fix the guide until setup takes under 60 minutes
- [ ] Test a backup restore and an upgrade from the previous version
- [ ] Run a test with 5 devices and confirm they appear on the dashboard

**First pilot**

- [ ] Confirm the pilot agreement is signed
- [ ] Install on the customer's server over a screen share, in `report_only` mode
- [ ] Enroll at least 5 developers, and confirm usage and PR data are arriving
- [ ] Record the start date for the baseline week

**Check:** The clean-VM install takes under 60 minutes, restore and upgrade both work, and the first pilot is sending data.

## Step 6: Rerun the benchmark and start the second pilot, December 14–20

The goal is evidence a skeptical buyer can check, even if the results are less impressive.

**Fix the harness**

- [ ] In `benchmarks/harness/runner.py`, stop passing `target_files` to the treatment arm
- [ ] Remove the harness's own malicious call, so security results reflect what the agent actually did
- [ ] Stop replacing old results with `INSERT OR REPLACE`, and keep every run
- [ ] Remove file names from task prompts
- [ ] Add Claude Code and Codex runners with shell access, using the real adapters
- [ ] Record token types and cost for every run

**Run it**

- [ ] Write down the tasks, metrics and token budget before starting
- [ ] Choose at least 3 public repos you didn't write
- [ ] Run at least 3 repeats per task and arm, in randomized order
- [ ] Report median cost change, the spread, success rate and all failures
- [ ] Rewrite the security suite as real attack attempts, with the baseline measured the same way
- [ ] Publish the raw results and update the README, even where Chronos doesn't win

**Second pilot**

- [ ] Fix the problems found during the first install
- [ ] Install the second pilot in `report_only` mode
- [ ] Stop at two active pilots until you've handled the first paid decision

**Check:** Anyone can rerun the benchmark from the repo, all runs are published, and the second pilot is sending data.

## Step 7: Run the pilot and get the paid decision, December 7–January 6

The goal is a written yes or no from the buyer, based on the targets agreed at the start. Dates below follow a first pilot starting on December 7.

**Days 1–7: Baseline**

- [ ] Keep Chronos in `report_only` mode, with no blocking or injected context
- [ ] Record the median cost per merged PR, merge rate and reverts
- [ ] Review draft policies with the buyer, using what the logs show

**Days 8–23: Active**

- [ ] Turn on context injection for a random 80% of branches
- [ ] Turn on policies in warning mode first, then blocking once false alarms are low
- [ ] Send a weekly report on cost, waste, blocks and false alarms
- [ ] Ask developers to mark false alarms, and fix the policy or the rule
- [ ] Fix urgent problems within one working day

**Days 24–30: Decision**

- [ ] Compare active and comparison branches on cost per merged PR, turns and PR outcomes
- [ ] Write a one-page result, including what didn't work and how uncertain the numbers are
- [ ] Review it with the buyer by day 28
- [ ] Ask for a written decision on the agreed paid terms by day 30

**If they say yes**

- [ ] Confirm the company and invoice requirements with your CA
- [ ] Sign the commercial license and issue a paid license file
- [ ] Send the first invoice manually

**If they say no**

- [ ] Record the exact reason: savings, price, security, install effort or lack of need
- [ ] Offer an uninstall and confirm their data stays with them

**Decide what comes next, once both pilots end**

- [ ] Continue if at least one team pays and the data shows lower cost per merged PR
- [ ] Change the pitch or target buyer if teams value audit and guardrails but savings are weak
- [ ] Pause and reassess if neither team will pay
- [ ] Choose the next feature from pilot evidence, considering vendor hosting, SSO, Cursor and shared locks

**Check:** You have a written decision from each pilot team and a clear decision on what to build next.
