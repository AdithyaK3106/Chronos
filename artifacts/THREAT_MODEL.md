# Chronos Threat Model & Security Boundaries

## 1. Security Boundaries

Chronos implements defense-in-depth across three execution boundaries:

1. **PreToolUse Lifecycle Interception:**
   Every tool invocation executed by an OpenHands SDK agent is intercepted by `runtime/hooks.py` before execution.
   If an agent requests an unauthorized path, attempts path traversal, violates read-only permissions, touches an unapproved protected gate, or edits a locked node, the hook issues `HookDecision.DENY`.
   OpenHands SDK conversation state marks the action as blocked (`blocked_actions`), partitions it out in `_ActionBatch.prepare`, and emits a `UserRejectObservation`. The underlying tool function is **never executed**.

2. **Canonical Path Sanitization:**
   Path evaluations use `pathlib.Path.resolve()`, enforcing that:
   - Relative traversals (`../../etc/passwd`) are resolved to their true target and checked against repository bounds.
   - Symlinks are resolved to their real disk target to prevent symlink escape attacks.
   - Sensitive file patterns (`.env*`, `*.pem`, `id_rsa*`, `.git/**`) are denied by default.

3. **Sub-File AST Node Mutual Exclusion:**
   Concurrency conflicts are guarded by SQLite `IMMEDIATE` transactions. Two agents racing for the same AST node cannot both acquire it due to primary key constraints and atomic transaction isolation.

---

## 2. Unbypassable Guarantees vs Known Enforcement Limitations

### Unbypassable Guarantees
- **Tool Interception:** Any tool call initiated via OpenHands SDK or Chronos MCP passes through deterministic policy checks.
- **Fail-Closed Semantics:** If an error occurs during policy parsing, permission evaluation, or gate verification, Chronos returns `DENY`.
- **Tamper-Evident Auditability:** Every authorization decision and ledger modification is hash-chained with SHA-256. Any modified byte in `chronos-audit.log` causes verification failure.
- **Zero Raw Secret Exposure:** API keys and credential tokens are scrubbed using regular expressions before writing to audit logs.

### Known Limitations & Mitigation
- **Raw OS Subshell Execution:** If an agent is granted unfettered shell access via `terminal` and runs commands like `python3 -c "import os; ..."` without path flags, lexical tool interception cannot introspect arbitrary runtime memory.
  - *Mitigation:* In high-security production environments, combine Chronos with container-level sandboxing (Docker/gVisor) and disable arbitrary shell tools for untrusted agents.
- **Prompt Injection:** An LLM may be instructed by hostile input to attempt malicious actions.
  - *Mitigation:* Chronos treats all LLM outputs as untrusted. Security is enforced deterministically in Python code at the runtime execution boundary, never by asking the LLM to police itself.
