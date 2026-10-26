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

---

### Recipe 2: Autonomous Problem Solving via CLI (`chronos run`)

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

### Recipe 3: Epistemic Living Memory (`chronos memory`)

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

### Recipe 4: Opt-In OpenHands CodeScout-1.7B SLM Localization

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
