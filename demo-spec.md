# Chronos Demo Spec
### Three formats, detailed feature requirements — no code

---

## Option 1: Side-by-Side Terminal Demo

**Goal:** Make the token cost argument viscerally real in under 3 minutes. No explanation needed — the numbers speak.

**Format:** Two tmux panes running simultaneously, both visible on screen. Scripted but running live against a real repo.

---

### The Repo

A mid-size Python API repo (~2,000 LOC) with:
- At least one major refactor in its git history — e.g., migration from `requests` to `httpx`, or from a raw DB query pattern to an ORM layer
- The deprecated symbol still present in some files (so the agent can "find" and reuse it)
- A handful of functions with non-obvious call graphs (so the agent actually needs to explore to understand them)
- The refactor commit clearly timestamped in git history (so Chronos's bi-temporal query can answer "as of before the refactor")

---

### Left Pane: Agent WITHOUT Chronos

**What it shows:**

A Claude Code (or Cursor) agent session working against the raw repo. The agent is given a task: "Add a new endpoint that calls the payment processing service."

The agent must explore the codebase from scratch. Show every tool call it makes:

- `read_file` on 8–12 files as it explores the repo structure
- `grep_search` 4–6 times looking for the payment service pattern
- `read_file` again on files it partially read earlier
- Final output: code that uses the deprecated `requests`-based pattern (because that's what most of the codebase still shows)
- Token counter in the pane header, updating in real time, climbing to ~18,000–25,000 tokens by the end

**What the counter tracks:**
- Total tokens consumed (running total)
- Tool calls made (running count)
- Time elapsed

**Key moment:** Agent produces code using `requests.post(PAYMENT_URL, ...)` — the pattern that was deprecated 3 weeks ago. No error. No warning. It looks correct.

---

### Right Pane: Agent WITH Chronos

**What it shows:**

Same agent, same task, same repo — but with Chronos MCP running.

The agent's first move is to call Chronos's `query_call_graph` tool instead of reading files. It gets back the full call graph for the payment service in one response. It calls `query_as_of` to confirm the current pattern (post-refactor) is `httpx`-based. Two tool calls total.

Final output: code that uses `httpx.post(...)` — the correct, current pattern.

Token counter shows ~1,200–1,800 tokens total.

**What the counter tracks:**
- Total tokens consumed
- Tool calls made
- Time elapsed

**Key moment:** When the right pane finishes before the left pane is even halfway through its file reads.

---

### The Token Counter Widget

Displayed in the header of each pane. Updates live as tool calls fire.

**Fields to show:**
- `TOKENS: 0` → climbs to final number
- `TOOL CALLS: 0` → climbs
- `ELAPSED: 0:00`

At the end, the two panes freeze and a summary line appears between them:

```
WITHOUT CHRONOS    |  WITH CHRONOS
22,847 tokens      |  1,340 tokens
31 tool calls      |  2 tool calls
2m 14s             |  18s
         94% cheaper. 15x faster.
```

---

### The Enforcement Moment

After the left pane finishes (with the wrong code), trigger a simulated CI run in the left pane. The CI check catches the deprecated `requests` usage. Show the PR block:

```
✗ BLOCKED  chronos-enforce
  Rule: no-requests-library (promoted 2026-08-01)
  Match: src/payments/client.py:47
  Pattern: requests.post(...)
  Fix: use httpx.AsyncClient — see docs/migration-guide.md
```

Right pane never hits CI because the agent used the correct pattern from the start.

---

### Script Flow (timing)

| Time | Left pane | Right pane |
|---|---|---|
| 0:00 | Agent receives task | Agent receives task |
| 0:05 | Starts reading files | Calls `query_call_graph` |
| 0:18 | Still reading (tool call 8) | Done. Writes correct code. |
| 1:30 | Finishes reading, writes code | — |
| 2:00 | CI runs, PR blocked | — |
| 2:15 | Summary card appears | — |

---

### Technical Requirements (no code — for Claude Code to implement)

- A tmux layout script that opens two panes with identical dimensions and a shared header bar
- A token counter process that reads tool call logs from both sessions and updates the pane headers in real time
- A pre-configured `.mcp.json` for the right pane that points to the local Chronos MCP server
- The demo repo pre-indexed by Chronos before the demo starts (so the first tool call returns instantly)
- The left pane session must be scripted/reproducible — not live freewheeling, so the demo doesn't vary. Consider a session replay tool (e.g., `asciinema` for the left pane, live for the right)
- CI enforcement output must be a real `chronos enforce` call, not mocked
- A single `make demo` command that starts everything: indexes the repo, opens tmux, starts both sessions

---
---

## Option 2: Chronos Dashboard

**Goal:** A browser-based dashboard that shows Chronos working in real time. For investor meetings, CDO pitches, and anyone who won't watch a terminal.

**Format:** Single-page web app served by `dashboard_server.py`. Dark theme. Opens at `localhost:7823`. No login, no setup beyond `chronos serve`.

---

### Layout

Three-column layout at the top (stat tiles), below that a two-column split (left: activity feed, right: enforcement events), below that a full-width graph freshness bar.

---

### Stat Tiles (top row, 4 tiles)

**Tile 1 — Graph Freshness**
- Label: "Last indexed"
- Value: time since last successful index (e.g., "3 seconds ago", "2 minutes ago")
- Subtext: number of files indexed in last run (e.g., "1,247 files")
- Status dot: green if <5 min, yellow if 5–15 min, red if >15 min
- Updates live via websocket

**Tile 2 — Token Savings (This Session)**
- Label: "Tokens saved"
- Value: estimated tokens saved vs. grep-and-read baseline, this session (e.g., "142,800")
- Subtext: "across 12 agent queries"
- Calculation method: (tool calls that would have been needed × avg tokens per file read) minus actual tokens used. Display the formula as a tooltip.
- Updates live as agents query the MCP server

**Tile 3 — Rules Active**
- Label: "Active rules"
- Value: count of rules in `warn-only` or `blocking` state
- Subtext: breakdown — e.g., "4 blocking · 7 warn-only"
- Clicking opens the Rules panel (see below)

**Tile 4 — Enforcement Events (This Week)**
- Label: "Enforced this week"
- Value: count of CI enforcement events (blocks + warns) in the last 7 days
- Subtext: delta vs. last week (e.g., "↑ 3 from last week")
- Color: neutral (this is not a bad number — enforcement working is good)

---

### Activity Feed (left column, below tiles)

A live-scrolling list of agent activity events pulled from the intent ledger (Wedge 3). Most recent at top.

**Each event card shows:**
- Agent identifier (e.g., "claude-code · session #14")
- Action type: `QUERY`, `LOCK_ACQUIRED`, `LOCK_RELEASED`, `WRITE_INTENT`
- Target: the node/file/function the agent acted on (e.g., `payments/client.py → process_payment()`)
- Timestamp (relative: "2s ago", "1m ago")
- Token cost for the operation (e.g., "340 tokens")

**Filters at top of feed:**
- All / Queries only / Writes only / Conflicts only
- Time range: Last hour / Today / This week

**Empty state:** "No agent activity yet. Connect an MCP client to start."

---

### Enforcement Events (right column, below tiles)

A list of CI enforcement events from Wedge 4. Most recent at top.

**Each event card shows:**
- Verdict badge: `BLOCKED` (red) or `WARNED` (yellow)
- PR reference: "PR #47" (clickable if GitHub URL is configured)
- Rule that fired: rule ID + short description
- Match location: file path + line number
- Agent that triggered it (from provenance ledger)
- Timestamp

**Clicking an event expands it to show:**
- The matched code pattern (the specific line that fired)
- The fix hint from the rule definition
- Which agent session produced the code
- What the agent queried from Chronos before writing the code (full query history from Wedge 3)

---

### Graph Freshness Bar (full width, below both columns)

A timeline showing index runs over the last 24 hours.

- X axis: time (24 hours)
- Each index run shown as a vertical bar: green = success, red = failure, yellow = partial (some files failed to parse)
- Hovering a bar shows: timestamp, files indexed, files failed, duration
- A "SLA line" drawn at the 5-minute mark — any gap wider than 5 minutes between runs is highlighted

---

### Rules Panel (slide-in drawer, opened from Tile 3)

A table of all rules in `chronos.db`.

**Columns:**
- Rule ID
- Description (short)
- State: `proposed` / `warn-only` / `blocking`
- Source: `git-native` or `packmind`
- Promoted by (human name from git commit)
- Times fired (last 30 days)
- Last fired (timestamp)

**Actions per row:**
- Demote (blocking → warn-only)
- Archive (remove from active enforcement)
- View full rule definition

**No rule creation from the dashboard.** Rules are authored via CLI or Wedge 2. Dashboard is read-only for rules.

---

### Token Savings Detail (click-through from Tile 2)

A breakdown of how token savings are calculated per agent session.

**Table:**
- Session ID
- Queries made to Chronos
- Estimated tokens if grep/read instead (calculated: queries × avg file read cost)
- Actual tokens used
- Savings (delta)
- Savings % 

Below the table: a methodology note explaining the baseline calculation so it's auditable, not a black box.

---

### Live Update Mechanism

- Dashboard connects to `dashboard_server.py` via WebSocket on load
- Server pushes events as they happen: new index runs, new agent activity, new enforcement events
- If WebSocket drops, banner appears: "Reconnecting…" — auto-reconnects every 5 seconds
- No page refresh needed for any data

---

### Settings (gear icon, top right)

- GitHub base URL (for clickable PR links in enforcement events)
- Baseline token cost per file read (for savings calculation — default: 800 tokens)
- Time zone display preference
- No auth settings — dashboard is local-only by design

---
---

## Option 3: Demo Repo + Scripted Scenario

**Goal:** A self-contained repo that any design partner can clone, run `chronos serve`, and immediately see a working Chronos instance with real data. Doubles as the onboarding path.

**Format:** A public GitHub repo (`chronos-demo`) with a fully pre-configured scenario. Everything works out of the box with one command.

---

### The Scenario: "NovaPay" — A Fictional Payments API

A fake but realistic Python/FastAPI e-commerce payments API. ~1,500 LOC across ~20 files. Realistic enough that engineers recognize the patterns. Simple enough that the demo scenario is obvious.

**The repo has three layers of history built into it:**

**Layer 1 — The Old World (commits 1–15)**
Uses `requests` library for all HTTP calls. Raw SQL queries via `psycopg2`. No type hints. All patterns that have since been deprecated.

**Layer 2 — The Refactor (commits 16–20)**
Migration to `httpx` for async HTTP. Migration to SQLAlchemy ORM. Type hints added across all new code. These commits are the "refactor event" that Chronos's bi-temporal graph tracks.

**Layer 3 — Current State (commits 21–25)**
Mixed codebase — some files fully migrated, some still on the old pattern (realistic). The deprecated patterns are still detectable by Chronos.

---

### Pre-Configured Chronos State

The repo ships with a pre-built `.chronos/` directory containing:

**Pre-indexed graph:**
- The bi-temporal graph already built against the repo's full git history
- Temporal facts covering all three layers
- Agents can immediately query "what did `process_payment` look like before the refactor?"

**Pre-authored rules (4 rules):**

| Rule ID | Description | State |
|---|---|---|
| `no-requests` | Don't use `requests` library — use `httpx` | blocking |
| `no-raw-sql` | Don't write raw SQL — use SQLAlchemy ORM | warn-only |
| `require-type-hints` | New functions must have type hints | warn-only |
| `no-direct-db-in-routes` | Routes must not import db models directly | blocking |

These rules are in `enforcement_rules` in `chronos.db` — live, real rules, not mocked.

**Pre-populated intent ledger:**
- 3 simulated agent sessions already in the provenance table, showing realistic activity
- Session 1: a query-only session (no writes)
- Session 2: a write session that used the correct patterns (all good)
- Session 3: a write session that was blocked by CI (the `no-requests` rule fired)

This means the dashboard opens with populated data on first launch — no "empty state" awkwardness.

---

### The Scripts

**`make demo-start`**
- Starts `chronos serve` (MCP server)
- Starts `dashboard_server.py`
- Opens the dashboard in the default browser
- Prints the MCP config block to copy into Claude Code / Cursor

**`make demo-scenario-1` — "The Deprecated Pattern"**
Replays a scripted agent session (using `asciinema` or equivalent) showing an agent being blocked from using `requests`. Ends with the enforcement event appearing live on the dashboard.

**`make demo-scenario-2` — "Time Travel Query"**
Opens an interactive prompt where the user can type: `chronos query --as-of "2026-07-01" --symbol process_payment`
The response shows the old `requests`-based implementation. Then queries current state — shows `httpx`. Demonstrates the bi-temporal graph live.

**`make demo-scenario-3` — "Token Comparison"**
Runs two real agent queries against the repo: one using grep/read (counts tool calls and tokens), one using Chronos (counts tool calls and tokens). Prints the side-by-side summary.

**`make demo-reset`**
Resets the demo to its pristine state — wipes any changes made during the demo, restores the pre-built graph and rules. So the demo can be run again cleanly.

---

### The `README.md` for the Demo Repo

Structure (not content, just sections):

1. **What this is** — one paragraph, what NovaPay is and why it exists
2. **What you'll see** — the three scenarios, described in plain English
3. **Prerequisites** — what needs to be installed (just Chronos + Python)
4. **Quick start** — `git clone` → `pip install chronos` → `make demo-start`
5. **Scenario walkthroughs** — one section per `make demo-scenario-*` with what to look for
6. **Connecting your own agent** — the MCP config block, how to point Claude Code or Cursor at it
7. **What's real vs. pre-built** — transparency section: which data is pre-populated and why

---

### `.mcp.json` shipped in the repo

Pre-configured MCP client config that any Claude Code or Cursor user can copy into their own config file. Points to the local Chronos server. Includes all tool descriptions so the agent knows what to call.

---

### ESLint / Semgrep Configs (for the auto-bootstrap demo)

The repo also ships with:
- `.semgrep/` directory with rules that match the 4 enforcement rules above (same patterns, Semgrep syntax)
- `.eslintrc` (even though it's Python — include it anyway as if this were a monorepo with a frontend)
- A `pyproject.toml` with `ruff` config

This sets up the future **governance auto-bootstrap** demo: run `chronos init --auto-detect` and watch Chronos discover all these existing configs and propose to convert them into guardrails. Not built yet — but the raw material is there.

---

### Hosting

The demo repo should be fully runnable offline — no external services, no cloud calls required beyond the LLM endpoint (which users configure themselves). The pre-built graph and database are committed to the repo so `make demo-start` works with zero network access.

---

## Priority and Sequencing

| Option | Build time | Needed for | Do first? |
|---|---|---|---|
| Option 1 (terminal) | 2–3 days | First pitch, any live meeting | Yes — this week |
| Option 3 (demo repo) | 1 week | Partner onboarding, leave-behind, self-serve | Second |
| Option 2 (dashboard) | 1–2 weeks | Investor meetings, CDO pitches, broader go-to-market | Third |

Option 3 and Option 2 are complementary — the demo repo *uses* the dashboard. Build Option 3 first and the dashboard work can be layered in on top of the same repo.

---
---

## Addendum: F1-F7 Governance Features

Added after the options above were built. Not a rewrite of the spec —
this is what changed on top of it once agent identity, permissions, audit,
gates, anomaly detection, and sensitive-read tracking (`ai-governance-research.md`)
were implemented.

**Dashboard (Option 2):** four new panels below the original activity/enforcement
split — Gates (pending human approvals, with a copy-pasteable `chronos gates
approve <id>` command), Agents & permissions (manifests: read-only, path-scoped,
emergency-lock-allowed), Behavioural anomalies (flagged sessions + which agents
are still in learning mode), and Sensitive reads. Plus a governance stat row
(active agents, audit chain valid/tampered, gates pending, anomalies this week).
All read-only over the same `chronos.db`, same 2s poll loop as the rest of the
dashboard — no new live-update mechanism.

**Demo repo (Option 3):** `demo/seed_governance.py` (new, run via `make
demo-fixture`) seeds three agents onto NovaPay — `claude-code`/`cursor`
unrestricted, `intern-bot` read-only and scoped to `src/payments/**` — plus a
pending gate on `src/payments/secrets/**` and a flagged anomaly from a seeded
9-day history, so the new panels aren't empty on first open. `make
demo-scenario-4` ("Human in the Loop") is a new scripted scenario: an agent's
lock request on payment-provider credentials is blocked by the gate, a human
approves it via `chronos gates approve`, the agent's retry succeeds.
