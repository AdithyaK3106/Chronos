"""Unit tests for ChronosToolGateway execution-boundary enforcement (Phase 3)."""

from pathlib import Path
import sqlite3
import pytest

from chronos.audit.logger import verify_audit_chain
from chronos.coordination.ledger import acquire_lock
from chronos.security.permissions import set_permissions
from chronos.verification.rule_store import upsert_rule, promote_rule
from runtime.gateway import ChronosToolGateway, ChronosSecurityVetoError

class MockTool:
    name = "file_editor"
    description = "Mock file editor for testing"

    def __init__(self):
        self.executed_actions = []

    def __call__(self, action, conversation=None):
        self.executed_actions.append(action)
        return {"status": "success", "action": action}

class MockAction:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self):
        return self._data

def test_gateway_permits_authorized_action(memory_db, tmp_path):
    log_file = tmp_path / "audit.log"
    set_permissions(memory_db, "agent_ok", {"allowed_paths": ["src/**"], "read_only": False})

    underlying = MockTool()
    gw = ChronosToolGateway(
        underlying_tool=underlying,
        con=memory_db,
        agent_id="agent_ok",
        repo_root=tmp_path,
        log_path=log_file,
    )

    action = MockAction({"path": "src/main.py", "content": "x = 42"})
    res = gw(action)

    assert res["status"] == "success"
    assert len(underlying.executed_actions) == 1

    # Verify audit event recorded
    valid, _, count = verify_audit_chain(log_file)
    assert valid is True
    assert count == 1

def test_gateway_blocks_path_traversal(memory_db, tmp_path):
    log_file = tmp_path / "audit.log"
    set_permissions(memory_db, "agent_bad", {"allowed_paths": ["src/**"]})

    underlying = MockTool()
    gw = ChronosToolGateway(
        underlying_tool=underlying,
        con=memory_db,
        agent_id="agent_bad",
        repo_root=tmp_path,
        log_path=log_file,
    )

    action = MockAction({"path": "../../etc/shadow", "content": "bad"})
    with pytest.raises(ChronosSecurityVetoError) as exc_info:
        gw(action)

    assert "escapes repository root" in str(exc_info.value)
    # Crucial assertion: Underlying tool NEVER executed
    assert len(underlying.executed_actions) == 0

def test_gateway_blocks_locked_node(memory_db, tmp_path):
    log_file = tmp_path / "audit.log"
    node_id = "src/payment.py::charge::Function"
    acquire_lock(memory_db, node_id, agent_id="agent_1", session_id="s1", intent="refactor")

    underlying = MockTool()
    gw = ChronosToolGateway(
        underlying_tool=underlying,
        con=memory_db,
        agent_id="agent_2",
        repo_root=tmp_path,
        log_path=log_file,
    )

    # Inferred lock test
    action = MockAction({"command": "str_replace", "path": "src/payment.py", "old_str": "a", "new_str": "b"})
    with pytest.raises(ChronosSecurityVetoError) as exc_info:
        gw(action)

    assert "locked by agent 'agent_1'" in str(exc_info.value)
    assert len(underlying.executed_actions) == 0

def test_gateway_blocks_blocking_rule_violation(memory_db, tmp_path):
    log_file = tmp_path / "audit.log"
    upsert_rule(memory_db, "rule-no-eval", "python", "No eval", yaml_pattern=r"eval\(", detectability_passed=True)
    promote_rule(memory_db, "rule-no-eval", "sec_lead")

    underlying = MockTool()
    gw = ChronosToolGateway(
        underlying_tool=underlying,
        con=memory_db,
        agent_id="agent_coder",
        repo_root=tmp_path,
        log_path=log_file,
    )

    action = MockAction({"command": "create", "path": "src/calc.py", "file_text": "res = eval(user_input)"})
    with pytest.raises(ChronosSecurityVetoError) as exc_info:
        gw(action)

    assert "Violated blocking rule" in str(exc_info.value)
    assert len(underlying.executed_actions) == 0
