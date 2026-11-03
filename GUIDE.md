# ⚡ Chronos: Zero-to-Hero Quickstart Guide (Under 2 Minutes)

Get up and running with **Chronos**—the bi-temporal repository intelligence, autonomous context resolution, and multi-agent governance engine—in under two minutes.

---

## ⏱️ The 2-Minute Stopwatch

### 00:00 – 00:30 | Step 1: One-Command Installation

Run this single command in your terminal. It automatically configures an isolated environment, installs polyglot Tree-sitter parsers, links `chronos` to your PATH, and runs diagnostic checks:

```bash
curl -fsSL https://raw.githubusercontent.com/chronos-agent/chronos/main/install.sh | bash
```

*Or if you have already cloned the repository:*
```bash
./install.sh
```

#### Install as a tool (no clone needed)

The core install (hooks, MCP server, indexer, resolver, firewall, memory, dashboard) is about 73 MB and
does not include the OpenHands agent stack. Add extras only if you need them:

```bash
uv tool install chronos-agent                  # core: Claude Code hooks + MCP, `chronos sync/resolve/ui`
uv tool install 'chronos-agent[agent]'         # + `chronos run` (headless OpenHands agent, ~2 GB)
uv tool install 'chronos-agent[all]'           # + agent runtime and the Kuzu graph backend
# pipx works the same way:  pipx install 'chronos-agent[agent]'
# From a local checkout:    uv tool install /path/to/chronos
```

> The distribution is named **`chronos-agent`** because `chronos` on PyPI is an unrelated project.
> The import name and the command are still `chronos`. It is not published to PyPI yet; until it
> is, install from a checkout.

`./install.sh` (from a clone) is the contributor setup and installs everything, including test tools.

#### Customization Options & Scriptable Flags
You can run the installer interactively or pass flags for unattended / CI setup:

| Flag | Description | Default |
|---|---|---|
| `-y`, `--yes` | Unattended mode (uses defaults without interactive prompts) | `false` |
| `--with-slm` | Opt-in to OpenHands CodeScout-1.7B SLM localization | `disabled` |
| `--no-slm` | Keep pure deterministic AST + BM25 (<60ms, zero extra memory) | **Enabled** |
| `--slm-backend=...` | SLM engine: `openai_compatible` (vLLM/remote), `ollama` (local), or `agy` | `openai_compatible` |
| `--slm-endpoint=...` | Base URL for SLM server (e.g. `http://localhost:11434` for Ollama) | `http://localhost:8000/v1` |
| `--no-mcp` | Skip automatic Cursor and Claude Desktop MCP configuration | Configures detected IDEs |
| `--dir=PATH` | Target directory for Chronos repository | `~/.chronos/repo` or current dir |

---

### 00:30 – 00:45 | Step 2: Verify System Health

Run the diagnostic check to ensure the SQLite WAL database, Tree-sitter polyglot grammars, OpenHands SDK, and hardware accelerators are ready:

```bash
chronos doctor
```

**Expected output:**
```text
Chronos Doctor Report:
---------------------
Repository Root : /path/to/your/repo
Chronos Dir     : OK (.chronos/)
SQLite WAL DB   : OK (23 tables)
Tree-Sitter     : OK (Python, TypeScript, JavaScript, Go)
OpenHands SDK   : OK (v1.58.2)

SLM Localization Engine:
-----------------------
SLM Active      : DISABLED (Default: Pure AST + BM25)
Target Model    : OpenHands/CodeScout-1.7B

Hardware Acceleration:
---------------------
GPU Detected    : NVIDIA GPU (or CPU Fallback)
NPU Detected    : None (Unverified / No accelerator found)
```

---

### 00:45 – 01:15 | Step 3: Index Your Repository

Index your project's codebase into the bi-temporal AST knowledge graph (< 2 seconds):

```bash
chronos sync
```

Chronos automatically parses **Python (`.py`)**, **TypeScript (`.ts`, `.tsx`)**, **JavaScript (`.js`, `.jsx`)**, and **Go (`.go`)**, resolving imports, function definitions, classes, and method call graphs.

---

### 01:15 – 02:00 | Step 4: First Zero-Path Resolution

Test Chronos's zero-path resolution by giving it a natural-language task prompt without specifying any filenames:

```bash
chronos resolve "Prevent race condition in lock ledger when releasing locks"
```

**Instant result (< 60ms):**
```text
🔍 Chronos Zero-Path Semantic Resolution [Deterministic AST + BM25]:
  Target Files    : chronos/coordination/ledger.py
  Target Symbols  : acquire_lock, check_conflicts, get_provenance, release_lock
  Associated Tests: tests/test_coordination.py
  Memories Injected: 0

#### 🔍 Retrieval Provenance & Relevance Rationale:
- list_active_locks (chronos/coordination/ledger.py): Score 26.0 — File stem matches term; BM25 rank -4.01
- release_lock (chronos/coordination/ledger.py): Score 21.4 — File stem matches term; BM25 rank -8.59
- Associated Tests Found: tests/test_coordination.py
```

🎉 **You are completely set up and ready to go!**

---

## 🎯 4 Instant Practical Recipes

### Recipe 1: Connect to Your IDE via MCP (Cursor, Claude Code, Windsurf)

Chronos runs as a high-performance **FastMCP 3.x** daemon, exposing bi-temporal tools directly to coding agents inside your editor.

#### 1. Start the MCP Server
```bash
chronos serve
```

#### 2. Editor Configuration
If not configured automatically by `install.sh`, add Chronos to your IDE's MCP config:

**Cursor (`~/.cursor/mcp.json`) or Claude Desktop (`claude_desktop_config.json`):**
```json
{
  "mcpServers": {
    "chronos": {
      "command": "chronos",
      "args": ["serve"]
    }
  }
}
```

#### 3. Agent Prompts to Try in Your IDE:
- *"What is the blast radius if I refactor `SQLiteGraphStore.ingest`?"* → Agent calls `as_of_impact`.
- *"Find all direct callers of `release_lock` across the repo."* → Agent calls `as_of_callers`.
- *"Acquire an AST write lease on `src/auth.py::login_handler` before editing."* → Agent calls `chronos_acquire_lock`.
- *"Recall any past gotchas or invariants about graph traversal."* → Agent calls `chronos_recall`.

#### 4. Claude Code: governance + automatic context (hooks + MCP)

MCP alone is *advisory*: the agent chooses whether to call Chronos. For enforcement and automatic
context injection, install the Claude Code hooks too (one command, idempotent, reversible):

```bash
chronos claude install --scope user      # hooks in ~/.claude/settings.json + `claude mcp add chronos`
chronos claude status                    # what is installed
chronos claude uninstall --scope user    # remove cleanly

cd your-repo && chronos init && chronos sync   # opt a repo in (repos without .chronos/ are never touched)
```

What you get inside an opted-in repo:

| Hook | Effect |
|---|---|
| `PreToolUse` (Read/Edit/Write/MultiEdit/NotebookEdit/Grep) | **Denies** secrets (`.env`, keys, credentials) and edits to code another agent has locked. Files outside the repository are left to Claude Code's own permissions unless `CHRONOS_HOOK_STRICT_BOUNDARY=1`, which denies them. **Asks you** before an edit to a protected module (auth, policy, security, permissions, ...). Reading protected code is always allowed. Every decision lands in the hash-chained audit log. |
| `UserPromptSubmit` | Injects the matching symbols, callers/callees, tests and recalled memories, only when the match is strong (threshold `CHRONOS_MIN_INJECT_SCORE`, default 12). It is labelled as untrusted reference data and capped in size, because it quotes repository text. |

Protected modules match whole words in the path: `auth.py`, `authService.ts` and `oauth_client.py`
are protected; `author.py` and `AUTHORS` are not. Add your own in `.chronos/protected.yml`.

Choose how strict to be with a preset:

```bash
chronos config set policy.preset strict     # relaxed | standard (default) | strict
```

| Preset | `protected_modules` | `on_hook_error` | `max_context_chars` |
|---|---|---|---|
| `relaxed` | `ask` | `allow` (fail open) | `8000` |
| `standard` | `ask` | `ask` | `6000` |
| `strict` | `deny` | `deny` (fail closed) | `4000` |

Secrets are denied under every preset. Settings written explicitly in `.chronos/config.json` override
the preset, e.g. `{ "policy": { "preset": "strict", "protected_modules": "ask" } }`. The individual
settings:

| Setting | Values | Default |
|---|---|---|
| `protected_modules` | `ask` (approve in Claude Code's prompt) or `deny` (until `chronos gate grant`) | `ask` |
| `on_hook_error` | `ask`, `deny` (strict) or `allow` (silent fail-open) | `ask` |
| `max_context_chars` | upper bound on injected context | `6000` |

Approve a protected edit (the denial message prints this command):

```bash
chronos gate grant auth.py --agent claude_code
chronos gate list
chronos gate revoke auth.py --agent claude_code
```

**Shell commands need the sandbox.** Hooks only see tool calls, so on their own they can't stop a
`Bash` command such as `sed -i` from editing a protected file. On Linux, start the agent with
`chronos exec` to enforce the same policy at the operating-system level:

```bash
chronos exec -- claude            # interactive Claude Code, sandboxed
chronos exec --dry-run            # show what would be protected
```

Inside the sandbox, protected modules are read-only (and their parent directories cannot be renamed),
secret files such as `.env` are unreadable, and Chronos's own policy files plus `.claude/settings.json`
are read-only, so the agent cannot switch its guardrails off. Files you approved with
`chronos gate grant` stay writable. Everything else in the repository stays writable, so this guards
protected files; it is not a general undo for destructive commands.

Requires bubblewrap (`sudo apt install bubblewrap`). On macOS and Windows `chronos exec` refuses to
start rather than running the agent unprotected; there, hooks alone apply.

Tunables: `CHRONOS_AGENT_ID`, `CHRONOS_CONTEXT_BUDGET` (tokens, default 1200),
`CHRONOS_HOOK_FAIL_CLOSED=1` (always deny if the hook itself errors, overriding `on_hook_error`),
`CHRONOS_HOOK_STRICT_BOUNDARY=1` (also deny paths outside the repo).

`chronos claude status` shows what is installed and runs each installed hook command once, so a
moved or broken install shows up as `"hook_commands_work": false`.

---

### Recipe 2: Chronos Control Plane & Observability Suite (`chronos ui`)

Launch the local web dashboard for deep visibility into agent execution, context assembly, and comparative benchmarks:

```bash
# Launch the dashboard with automatic browser opening (binds to port 8765)
chronos ui

# Specify a custom port or prevent automatic browser launch
chronos ui --port 9000 --no-browser

# Launch scoped to a specific repository
chronos ui --repo /path/to/another/repo
```

The Chronos Control Plane includes:
1. **Runs & Trace Explorer:** Searchable executions with duration, token counts, and progressive event span waterfalls.
2. **Context Inspector:** Inspect the exact context envelopes delivered to agents, including AST ranking signals and memory tiers.
3. **Evaluation Studio:** Side-by-side benchmark comparison (Baseline vs. Chronos) with paired latency, token savings, and CSV/JSON export.
4. **Mission Telemetry & Audit Chain:** Real-time tamper-evident SHA-256 hash chain verification and active lease management.

---

### Recipe 3: Autonomous Problem Solving via CLI (`chronos run`)

Execute end-to-end coding tasks with an OpenHands autonomous agent protected by Chronos pre-execution governance:

```bash
# 1. Using OpenAI (GPT-4o)
export OPENAI_API_KEY="sk-..."
chronos run "Fix race condition in lock ledger" --model=gpt-4o

# 2. Using Anthropic (Claude 3.7 Sonnet)
export ANTHROPIC_API_KEY="sk-ant-..."
chronos run "Refactor payment webhook processing" --model=claude-3-7-sonnet

# 3. Using Free Local Models (Ollama / vLLM)
chronos run "Add boundary checks to cylinder verification" \
  --model=ollama/qwen2.5-coder \
  --base-url=http://localhost:11434

# 4. Using Local Antigravity CLI Runner (Zero External API Keys)
chronos run "Implement regex-based symbol pattern filtering" --model=gemini-3.8-flash-low
```

---

### Recipe 4: Epistemic Living Memory (`chronos memory`)

Chronos maintains cross-session memory with 5 tiers (`core`, `learned`, `episodic`, `working`, `procedural`), recency decay, and verification weighting:

```bash
# Store a learned insight from a subtle bug
chronos memory remember "Cycle prevention in graph traversal requires tracking visited paths in recursive CTE" --tier learned

# Add a permanent behavioral procedure / team invariant
chronos memory remember "Always run pytest before committing code changes" --tier procedural

# Recall past lessons relevant to a current task
chronos memory recall "cycle prevention graph traversal"

# List stored memories filtered by tier
chronos memory list --tier learned

# View memory distribution and statistics
chronos memory stats
```

---

### Recipe 5: Opt-In OpenHands CodeScout-1.7B SLM Localization

For monorepos or complex natural-language tasks where keywords do not match filenames, toggle the CodeScout-1.7B neural localization engine:

```bash
# 1. Inspect current SLM configuration
chronos config show

# 2. Enable CodeScout-1.7B
chronos config set slm.enabled true

# 3. Select backend (openai_compatible for remote vLLM/LiteLLM, or ollama for local)
chronos config set slm.backend ollama
chronos config set slm.endpoint http://localhost:11434

# 4. Run zero-path resolution with SLM inference
chronos resolve "Sanitize credential keys in tamper-evident audit logs" --enable-slm

# 5. Disable SLM anytime to return to instant pure AST + BM25 (<60ms)
chronos config set slm.enabled false
```

---

## 🛡️ Security & Fail-Closed Guardrails

Chronos includes default pre-execution firewall protection for sensitive files (`*policy*`, `*auth*`, `*firewall*`, `*.env*`, `*credentials*`).

### Customizing Protected Patterns
Create `.chronos/protected.yml` in your repository root:

```yaml
policies:
  - id: core-security
    paths:
      - "src/auth/**"
      - "*policy*"
      - "*credentials*"
      - "*.env*"
    mode: human_approval
    denial_reason: "Security-critical module requires human gate approval."
```

### Verifying the Cryptographic Audit Trail
Ensure that all tool executions and blocked attempts are tamper-evident:
```bash
chronos audit verify
```
*(Outputs `Audit Chain Verification: PASSED (Records: N)`)*

The log lives outside the repository, in your user data directory (one file per repository; the
command prints its path), so the agent's tools and `git clean` cannot touch it. Set
`CHRONOS_DATA_DIR` to move it. A log left in `.chronos/chronos-audit.log` by an older version is moved
there on first use; the original is kept as `chronos-audit.log.migrated`.

---

## ❓ Frequently Asked Questions & Troubleshooting

### Q: Does Chronos send my code to third-party cloud servers?
**No.** Chronos is local-first. Tree-sitter parsing, AST call graph generation, SQLite FTS5 BM25 search, memory stores, and write lease coordination run 100% locally on your machine. External network calls only happen if you explicitly invoke an external LLM (e.g. OpenAI/Anthropic API).

### Q: How do I hide the OpenHands SDK banner?
Add this environment variable to your shell profile (`~/.bashrc` or `~/.zshrc`):
```bash
export OPENHANDS_SUPPRESS_BANNER=1
```

### Q: How do I uninstall Chronos?
Simply remove the binary link and repository:
```bash
rm -f ~/.local/bin/chronos
rm -rf ~/.chronos
```
