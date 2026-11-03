"""Chronos deterministic PreToolUse security interceptor for OpenHands SDK."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chronos.audit.logger import append_audit_event
from chronos.coordination.ledger import check_conflicts
from chronos.security.gates import check_gate_approval
from chronos.security.permissions import check_action_allowed
from chronos.security.sanitizer import validate_path_access
from chronos.verification.enforcer import evaluate_rules_on_content

def _extract_target_path(tool_name: str, tool_input: dict[str, Any] | None) -> str | None:
    """Extract file or directory target from tool parameters."""
    if not tool_input:
        return None
    for key in ("path", "file_path", "filename", "filepath", "target_path", "source_file"):
        if key in tool_input and isinstance(tool_input[key], str):
            return tool_input[key]
    return None

def _extract_content(tool_input: dict[str, Any] | None) -> str | None:
    """Extract content from tool parameters (content, file_text, new_str)."""
    if not tool_input:
        return None
    for key in ("content", "file_text", "new_str"):
        if key in tool_input and isinstance(tool_input[key], str):
            return tool_input[key]
    return None

def _check_inferred_locks(
    con: sqlite3.Connection,
    agent_id: str,
    target_path: str,
    repo_root: Path,
) -> tuple[bool, str]:
    """Check if any AST node belonging to target_path is locked by another agent.

    Node ids are ``<path>::<symbol>::<kind>`` where ``<path>`` is the absolute POSIX path
    produced by the indexer (older/explicit ids may be repo-relative). Only exact path
    prefixes are matched, so files that merely share a basename never conflict.
    """
    from chronos.security.sanitizer import sanitize_and_resolve_path
    try:
        resolved = sanitize_and_resolve_path(target_path, repo_root)
    except Exception:
        # Path escapes the repo or is invalid: earlier checks deny this; don't widen matching.
        return True, "No lock conflicts"

    prefixes = {
        f"{resolved.as_posix()}::",
        f"{resolved.relative_to(repo_root).as_posix()}::",
    }
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        rows = con.execute(
            "SELECT node_id, agent_id FROM intent_locks WHERE expires_at > ?",
            (now_iso,),
        ).fetchall()
    except Exception as e:
        # Fail closed: the lock table is part of the governance contract
        return False, f"Lock check failed closed: {e}"

    for row in rows:
        locked_node = row["node_id"] if isinstance(row, sqlite3.Row) else row[0]
        holder = row["agent_id"] if isinstance(row, sqlite3.Row) else row[1]
        if holder != agent_id and any(locked_node.startswith(px) for px in prefixes):
            return False, f"AST node '{locked_node}' in '{target_path}' is locked by agent '{holder}'"
    return True, "No lock conflicts"


# file_editor sub-commands that never modify the filesystem
READ_ONLY_COMMANDS = {"view"}


def _is_mutating_call(tool_name: str, tool_input: dict[str, Any] | None) -> bool:
    """Return False only for calls known to be read-only; unknown calls are treated as mutating."""
    cmd = tool_input.get("command") if tool_input else None
    if tool_name in ("file_editor", "edit_file", "write_file", "planning_file_editor"):
        return cmd not in READ_ONLY_COMMANDS
    return True


def evaluate_tool_security_detailed(
    con: sqlite3.Connection,
    agent_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    repo_root: str | Path,
    log_path: str | Path | None = None,
) -> tuple[bool, str, str]:
    """Like `evaluate_tool_security`, plus the deciding check.

    Category is one of: allow, permission, gate, lock, rule, audit, error. Hosts use it to pick a
    response, e.g. Claude Code may *ask* the user for a protected-module (gate) denial but must
    hard-deny secrets (permission) and lock conflicts.
    """
    root = Path(repo_root).resolve()
    target_path = _extract_target_path(tool_name, tool_input)

    try:
        # 1. Permission manifest & path traversal / symlink check
        is_allowed, reason = check_action_allowed(
            con=con,
            agent_id=agent_id,
            tool_name=tool_name,
            target_path=target_path,
            repo_root=root,
        )
        if not is_allowed:
            _log_decision(log_path, agent_id, tool_name, target_path or "none", "deny", reason, category="permission")
            return False, reason, "permission"

        is_mutating = _is_mutating_call(tool_name, tool_input)

        # 2. Gate approval check for sensitive modules (.chronos/protected.yml or default safety policies).
        #    Gates guard modification; reads are already constrained by the permission manifest
        #    and DEFAULT_DENIED_PATTERNS (secrets, .env, keys) in step 1.
        if target_path and is_mutating:
            is_approved, gate_reason = check_gate_approval(target_path, root, con=con, agent_id=agent_id)
            if not is_approved:
                _log_decision(log_path, agent_id, tool_name, target_path, "deny", gate_reason, category="gate")
                return False, gate_reason, "gate"

        # 3. Check AST node locks (explicit node_id or inferred target_path locks)
        if tool_input and "node_id" in tool_input:
            node_id = str(tool_input["node_id"])
            conflict_info = check_conflicts(con, [node_id])
            if node_id in conflict_info.get("locked", {}):
                holder = conflict_info["locked"][node_id]["agent_id"]
                if holder != agent_id:
                    lock_reason = f"AST node '{node_id}' is locked by agent '{holder}'"
                    _log_decision(log_path, agent_id, tool_name, node_id, "deny", lock_reason, category="lock")
                    return False, lock_reason, "lock"
        elif target_path and tool_name in ("file_editor", "edit_file", "write_file"):
            if is_mutating:
                lock_ok, lock_reason = _check_inferred_locks(con, agent_id, target_path, root)
                if not lock_ok:
                    _log_decision(log_path, agent_id, tool_name, target_path, "deny", lock_reason, category="lock")
                    return False, lock_reason, "lock"

        # 4. Content verification against blocking rules if content provided
        content = _extract_content(tool_input)
        if content:
            passed, violations = evaluate_rules_on_content(con, content, target_path or "unknown")
            if not passed:
                rule_reason = f"Violated blocking rule: {violations[0]['message']}"
                _log_decision(log_path, agent_id, tool_name, target_path or "content", "deny", rule_reason, category="rule")
                return False, rule_reason, "rule"

        if not _log_decision(log_path, agent_id, tool_name, target_path or "none", "allow", "All security checks passed", category="allow"):
            # Fail closed: an action that cannot be recorded in the audit trail is not executed
            return False, "Audit trail unavailable or corrupted; action denied (run `chronos audit verify`)", "audit"
        return True, "Allowed", "allow"

    except Exception as e:
        # Strict fail closed
        fail_closed_reason = f"Security interceptor failed closed: {e}"
        _log_decision(log_path, agent_id, tool_name, target_path or "error", "deny", fail_closed_reason, category="error")
        return False, fail_closed_reason, "error"

def evaluate_tool_security(
    con: sqlite3.Connection,
    agent_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    repo_root: str | Path,
    log_path: str | Path | None = None,
) -> tuple[bool, str]:
    """Deterministically evaluate security rules before tool execution. Fails closed."""
    allowed, reason, _ = evaluate_tool_security_detailed(
        con, agent_id, tool_name, tool_input, repo_root, log_path
    )
    return allowed, reason


def _log_decision(
    log_path: str | Path | None, agent_id: str, tool_name: str, target: str, status: str, reason: str,
    category: str = "",
) -> bool:
    """Append a decision to the audit trail. Returns False if it could not be recorded.

    `category` is the deciding check (allow, permission, gate, lock, rule, error) so readers such as
    `chronos ui` can tell a protected-module hold (which Claude Code turns into "ask") from a hard deny.
    """
    if not log_path:
        return True
    try:
        append_audit_event(
            log_path=log_path,
            event_type="tool_authorization",
            agent_id=agent_id,
            action=tool_name,
            target=target,
            status=status,
            reason=reason,
            metadata={"category": category} if category else None,
        )
        return True
    except Exception:
        return False

def run_hook_cli(con: sqlite3.Connection, agent_id: str, repo_root: str | Path, log_path: str | Path | None = None):
    """Entry point for OpenHands SDK shell hook executing via stdin/stdout."""
    try:
        raw_input = sys.stdin.read()
        event_dict = json.loads(raw_input) if raw_input.strip() else {}
        tool_name = event_dict.get("tool_name", "unknown")
        tool_input = event_dict.get("tool_input", {})

        allowed, reason = evaluate_tool_security(
            con=con,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            repo_root=repo_root,
            log_path=log_path,
        )

        decision_str = "allow" if allowed else "deny"
        output = {"decision": decision_str, "reason": reason}
        print(json.dumps(output))

        # OpenHands SDK: exit 2 signals blocking denial
        if not allowed:
            sys.exit(2)
        sys.exit(0)
    except Exception as e:
        # Fail closed on unexpected crash
        print(json.dumps({"decision": "deny", "reason": f"Fatal hook failure: {e}"}))
        sys.exit(2)
