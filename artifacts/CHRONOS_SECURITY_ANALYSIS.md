# Chronos: Mandatory Knowledge & Enforcement Architecture
## Security Analysis & Threat Model

**Status:** Security Baseline Specification  
**Scope:** OpenHands SDK v1.58.2 Execution Boundary & Chronos Governance Engine

---

## 1. Threat Model & Adversary Assumptions

We assume an **adversarial or non-compliant agent model** operating inside OpenHands:
- The model may ignore system instructions, prompts, and MCP tools.
- The model may attempt unauthorized file modifications, path traversals, or access to sensitive credentials (`.env`, private keys).
- The model may attempt to invoke `TerminalTool` to execute arbitrary bash scripts (`rm -rf`, `curl`, `eval`, Python one-liners).
- Multiple concurrent agents may attempt conflicting edits on overlapping AST nodes.
- OpenHands SDK internal components or hooks may throw unexpected exceptions, time out, or produce malformed JSON.

---

## 2. Bypass Routes & Architectural Defenses

| # | Attack Vector / Bypass Route | Mechanism & Vulnerability in Vanilla OpenHands | Chronos Defense & Enforcement Boundary |
| :-: | :--- | :--- | :--- |
| **1** | **Hook Failure "Fall-Open"** | OpenHands SDK `HookExecutor._parse_decision` explicitly falls open (`ALLOW`) if a hook crashes, produces no JSON, or exits with code != 2. | **Dual-Layer Fail-Closed Gateway**: `evaluate_tool_security` wraps execution in `try/except Exception` and exits with code 2. In addition, Layer 2 (`ChronosToolGateway`) independently verifies policy and raises `PermissionError` if unverified. |
| **2** | **Direct Tool Invocation** | If an agent is initialized without `HookConfig`, OpenHands executes all tools with zero interception. | **Mandatory Tool Wrapping**: `ChronosAgentRuntime.create_agent` automatically wraps every `Tool` with `ChronosToolGateway`. The raw tool is never registered unwrapped. |
| **3** | **Terminal / Shell Bypass** | If `TerminalTool` is enabled, an agent can bypass Python file hooks via `cat`, `echo >> file.py`, or `python -c "os.remove(...)"`. | **Container & OS Sandbox**: Terminal commands are run inside an unprivileged Linux container with a read-only rootfs, network egress disabled (`--net=none`), and `.chronos/` masked with read-only tmpfs. |
| **4** | **Path Traversal & Symlinks** | Agent requests `../../etc/shadow` or creates a symlink `link -> /etc/passwd` to read outside workspace. | **Canonical Path Sanitizer (`chronos/security/sanitizer.py`)**: Resolves real target path before every action, checks `.relative_to(repo_root)`, and verifies real symlink destinations. |
| **5** | **Sub-File Lock Omission** | `FileEditorTool` does not send `node_id`, allowing agents to bypass AST locks by omitting the field. | **Inferred AST Locking**: Chronos dynamically maps modified file line ranges to enclosing AST functions/classes in `SQLiteGraphStore` and validates lease ownership before allowing the edit. |
| **6** | **Credential Exposure** | Malicious prompt causes agent to output API keys or read `.env` files into conversation history. | **Secret Redaction Filter**: Sensitive regex patterns (`sk-`, `ghp_`, `PRIVATE KEY`, `.env*`) are scrubbed from all tool inputs, outputs, and audit logs. |
| **7** | **Audit Log Tampering** | Agent attempts to delete or alter `.chronos/chronos-audit.log` or `.chronos/chronos.db`. | **Cryptographic Hash Chaining + OS Masking**: Every entry contains a SHA-256 chain (`prev_hash`). Container sandbox mounts `.chronos/` as read-only to the agent process. |

---

## 3. Enforcement Guarantees: Application Layer vs Sandbox Boundary

```
+---------------------------------------------------------------------------------------------------+
|                                  SECURITY BOUNDARY ARCHITECTURE                                   |
|                                                                                                   |
|  [APPLICATION LAYER GUARANTEES (Chronos Runtime)]                                                 |
|  - Validates all registered OpenHands SDK tools (FileEditorTool, custom tools)                    |
|  - Enforces path sanitization, permission manifests, and protected gate approvals                 |
|  - Enforces sub-file AST node locking and multi-agent coordination                                |
|  - Scans content against AST verification rules (regex / syntax patterns)                         |
|  - Cryptographically records all decisions in append-only SHA-256 audit ledger                    |
|                                                                                                   |
|  ================================== BOUNDARY DIVISION ==========================================  |
|                                                                                                   |
|  [OS & CONTAINER SANDBOX GUARANTEES (Linux Kernel / Docker / Bubblewrap)]                         |
|  - Confines arbitrary subprocess and TerminalTool execution                                       |
|  - Enforces filesystem boundaries via mount namespaces and read-only rootfs (`/`, `/usr`, `/lib`) |
|  - Protects `.chronos/` database and audit log from filesystem deletion via read-only tmpfs mount |
|  - Enforces network air-gapping (`--net=none`) to prevent data exfiltration                       |
|  - Enforces resource limits (cgroups for memory, CPU, process count)                              |
+---------------------------------------------------------------------------------------------------+
```

---

## 4. Invariant Enforcement Matrix

| Invariant | Description | Enforcing Component | Failure Mode |
| :--- | :--- | :--- | :--- |
| **SEC-INV-01** | Repository Boundary Containment | `chronos.security.sanitizer` | Fails Closed (`PathTraversalError`) |
| **SEC-INV-02** | Sensitive Path Deny List (`.env*`, `secrets/**`) | `chronos.security.permissions` | Fails Closed (`PermissionDenied`) |
| **SEC-INV-03** | Gate Approval on Protected Modules | `chronos.security.gates` | Fails Closed (`ApprovalRequired`) |
| **SEC-INV-04** | Sub-File AST Node Mutual Exclusion | `chronos.coordination.ledger` | Fails Closed (`ConflictDetected`) |
| **SEC-INV-05** | Blocking AST Content Rules | `chronos.verification.enforcer` | Fails Closed (`RuleViolationVeto`) |
| **SEC-INV-06** | Cryptographic Audit Tamper-Evidence | `chronos.audit.logger` | Broken Chain Detection (`verify_audit_chain`) |
