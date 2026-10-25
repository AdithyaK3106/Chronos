"""Tests for OpenHands SDK PreToolUse hook interception semantics."""

from pathlib import Path
import pytest

from chronos.audit.logger import verify_audit_chain
from chronos.coordination.ledger import acquire_lock
from chronos.security.permissions import set_permissions
from chronos.verification.rule_store import upsert_rule, promote_rule
from runtime.hooks import evaluate_tool_security

def test_hook_permits_authorized_action(memory_db, tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    set_permissions(memory_db, "agent_1", {"allowed_paths": ["src/**"], "read_only": False})

    allowed, reason = evaluate_tool_security(
        con=memory_db,
        agent_id="agent_1",
        tool_name="file_editor",
        tool_input={"path": "src/main.py", "content": "x = 1"},
        repo_root=tmp_path,
        log_path=log_file,
    )
    assert allowed is True
    assert reason == "Allowed"

    # Verify audit event logged
    is_valid, _, count = verify_audit_chain(log_file)
    assert is_valid is True
    assert count == 1

def test_hook_blocks_path_traversal(memory_db, tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    set_permissions(memory_db, "agent_1", {"allowed_paths": ["src/**"]})

    allowed, reason = evaluate_tool_security(
        con=memory_db,
        agent_id="agent_1",
        tool_name="file_editor",
        tool_input={"path": "src/../../etc/shadow"},
        repo_root=tmp_path,
        log_path=log_file,
    )
    assert allowed is False
    assert "escapes repository root" in reason

def test_hook_blocks_locked_node(memory_db, tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    node_id = "src/api.py::checkout::Function"
    
    # Agent 2 holds the lock
    acquire_lock(memory_db, node_id, agent_id="agent_2", session_id="s2", intent="fix bug")

    # Agent 1 tries to edit that locked node
    allowed, reason = evaluate_tool_security(
        con=memory_db,
        agent_id="agent_1",
        tool_name="file_editor",
        tool_input={"path": "src/api.py", "node_id": node_id},
        repo_root=tmp_path,
        log_path=log_file,
    )
    assert allowed is False
    assert "locked by agent 'agent_2'" in reason

def test_hook_blocks_ast_rule_violation(memory_db, tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    upsert_rule(memory_db, "no-eval", "python", "No eval", yaml_pattern=r"eval\(", detectability_passed=True)
    promote_rule(memory_db, "no-eval", "sec_lead")

    allowed, reason = evaluate_tool_security(
        con=memory_db,
        agent_id="agent_1",
        tool_name="file_editor",
        tool_input={"path": "src/calc.py", "content": "res = eval(expr)"},
        repo_root=tmp_path,
        log_path=log_file,
    )
    assert allowed is False
    assert "Violated blocking rule" in reason
