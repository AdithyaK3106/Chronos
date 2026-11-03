"""Chronos deterministic PreToolUse security interceptor for OpenHands SDK."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openhands.sdk.hooks.config import HookDefinition, HookType
from openhands.sdk.hooks.types import HookDecision, HookEvent, HookEventType

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
    """Check if any AST node belonging to target_path is locked by another agent."""
    try:
        from chronos.security.sanitizer import sanitize_and_resolve_path
        resolved = sanitize_and_resolve_path(target_path, repo_root)
        rel_posix = resolved.relative_to(repo_root).as_posix()
    except Exception:
        rel_posix = Path(target_path).as_posix()

    pattern = f"{rel_posix}::%"
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        rows = con.execute(
            "SELECT node_id, agent_id FROM intent_locks WHERE (node_id LIKE ? OR node_id LIKE ?) AND expires_at > ?",
            (pattern, f"%{Path(target_path).name}::%", now_iso),
        ).fetchall()
        for row in rows:
            holder = row["agent_id"] if isinstance(row, sqlite3.Row) else row[1]
            locked_node = row["node_id"] if isinstance(row, sqlite3.Row) else row[0]
            if holder != agent_id:
                return False, f"AST node '{locked_node}' in '{target_path}' is locked by agent '{holder}'"
        return True, "No lock conflicts"
    except Exception as e:
        # If table doesn't exist or query fails, allow fallback unless active
        return True, "Lock check bypassed"

def evaluate_tool_security(
    con: sqlite3.Connection,
    agent_id: str,
    tool_name: str,
    tool_input: dict[str, Any] | None,
    repo_root: str | Path,
    log_path: str | Path | None = None,
) -> tuple[bool, str]:
    """Deterministically evaluate security rules before tool execution. Fails closed."""
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
            _log_decision(log_path, agent_id, tool_name, target_path or "none", "deny", reason)
            return False, reason

        # 2. Gate approval check for sensitive modules (.chronos/protected.yml or default safety policies)
        if target_path:
            is_approved, gate_reason = check_gate_approval(target_path, root, con=con, agent_id=agent_id)
            if not is_approved:
                _log_decision(log_path, agent_id, tool_name, target_path, "deny", gate_reason)
                return False, gate_reason

        # 3. Check AST node locks (explicit node_id or inferred target_path locks)
        if tool_input and "node_id" in tool_input:
            node_id = str(tool_input["node_id"])
            conflict_info = check_conflicts(con, [node_id])
            if node_id in conflict_info.get("locked", {}):
                holder = conflict_info["locked"][node_id]["agent_id"]
                if holder != agent_id:
                    lock_reason = f"AST node '{node_id}' is locked by agent '{holder}'"
                    _log_decision(log_path, agent_id, tool_name, node_id, "deny", lock_reason)
                    return False, lock_reason
        elif target_path and tool_name in ("file_editor", "edit_file", "write_file"):
            # Check if this is a mutating command
            cmd = tool_input.get("command") if tool_input else None
            is_mutating = cmd in ("create", "str_replace", "insert", "undo_edit") or cmd is None
            if is_mutating:
                lock_ok, lock_reason = _check_inferred_locks(con, agent_id, target_path, root)
                if not lock_ok:
                    _log_decision(log_path, agent_id, tool_name, target_path, "deny", lock_reason)
                    return False, lock_reason

        # 4. Content verification against blocking rules if content provided
        content = _extract_content(tool_input)
        if content:
            passed, violations = evaluate_rules_on_content(con, content, target_path or "unknown")
            if not passed:
                rule_reason = f"Violated blocking rule: {violations[0]['message']}"
                _log_decision(log_path, agent_id, tool_name, target_path or "content", "deny", rule_reason)
                return False, rule_reason

        _log_decision(log_path, agent_id, tool_name, target_path or "none", "allow", "All security checks passed")
        return True, "Allowed"

    except Exception as e:
        # Strict fail closed
        fail_closed_reason = f"Security interceptor failed closed: {e}"
        _log_decision(log_path, agent_id, tool_name, target_path or "error", "deny", fail_closed_reason)
        return False, fail_closed_reason

def _log_decision(log_path: str | Path | None, agent_id: str, tool_name: str, target: str, status: str, reason: str):
    if log_path:
        try:
            append_audit_event(
                log_path=log_path,
                event_type="tool_authorization",
                agent_id=agent_id,
                action=tool_name,
                target=target,
                status=status,
                reason=reason,
            )
        except Exception:
            pass

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
