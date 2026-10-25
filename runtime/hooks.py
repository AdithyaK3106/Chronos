"""Chronos deterministic PreToolUse security interceptor for OpenHands SDK."""

import json
import sqlite3
import sys
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

        # 2. Gate approval check for sensitive modules (.chronos/protected.yml)
        if target_path:
            is_approved, gate_reason = check_gate_approval(target_path, root)
            if not is_approved:
                _log_decision(log_path, agent_id, tool_name, target_path, "deny", gate_reason)
                return False, gate_reason

        # 3. Check AST node locks if node_id provided or inferred
        if tool_input and "node_id" in tool_input:
            node_id = str(tool_input["node_id"])
            conflict_info = check_conflicts(con, [node_id])
            if node_id in conflict_info.get("locked", {}):
                holder = conflict_info["locked"][node_id]["agent_id"]
                if holder != agent_id:
                    lock_reason = f"AST node '{node_id}' is locked by agent '{holder}'"
                    _log_decision(log_path, agent_id, tool_name, node_id, "deny", lock_reason)
                    return False, lock_reason

        # 4. Content verification against blocking rules if content provided
        if tool_input and "content" in tool_input and isinstance(tool_input["content"], str):
            content = tool_input["content"]
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
