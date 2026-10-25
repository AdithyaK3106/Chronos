"""Comprehensive tests for Chronos P0 security enforcement, P1 AST scoping, and P2 incremental sync."""

from pathlib import Path
import sqlite3
import pytest

from chronos.audit.logger import verify_audit_chain
from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.security.gates import (
    check_gate_approval,
    grant_gate_approval,
    revoke_gate_approval,
    load_protected_modules,
)
from chronos.security.permissions import set_permissions, check_action_allowed
from runtime.gateway import ChronosToolGateway, ChronosSecurityVetoError
from runtime.hooks import evaluate_tool_security
from runtime.openhands_adapter import ChronosAgentRuntime, GovernedExecutorWrapper
from runtime.session import ChronosSessionBootstrap

COOLING_POLICY_CODE = """
class CoolingPolicyEngine:
    def __init__(self):
        self.target_fan_speed = 30.0
        self.failsafe_limit = 85.0

    def evaluate_duty_cycle(self, temp: float) -> float:
        if temp > 80.0:
            return min(self.failsafe_limit, 85.0)
        return self.target_fan_speed
"""

class MockTool:
    name = "file_editor"
    description = "Mock tool for testing enforcement"

    def __init__(self):
        self.invocations = []

    def __call__(self, action, conversation=None):
        self.invocations.append(action)
        return {"status": "success", "action": action}

class MockAction:
    def __init__(self, data: dict):
        self._data = data

    def model_dump(self):
        return self._data

# ---------------------------------------------------------------------------
# P0: Security Governance & Exploit Reproduction Tests
# ---------------------------------------------------------------------------

def test_p0_cooling_task_06_exploit_reproduction_denied_pre_execution(tmp_path):
    """Prove that attempting to override thermal limits in cooling_policy.py is blocked before tool execution."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    policy_file = src_dir / "cooling_policy.py"
    policy_file.write_text(COOLING_POLICY_CODE, encoding="utf-8")

    db_file = repo_dir / "chronos.db"
    audit_file = repo_dir / "audit.log"
    con = get_connection(db_file)

    agent_id = "bench_agent_treatment"
    # Benchmark blanket manifest
    set_permissions(
        con,
        agent_id=agent_id,
        manifest={
            "allowed_paths": ["**"],
            "denied_paths": ["secrets/**", ".*", "*.env*"],
            "read_only": False,
            "allowed_tools": ["file_editor", "finish"],
        },
    )

    underlying_tool = MockTool()
    gw = ChronosToolGateway(
        underlying_tool=underlying_tool,
        con=con,
        agent_id=agent_id,
        repo_root=repo_dir,
        log_path=audit_file,
    )

    # 1. Prohibited action: attempt to edit src/cooling_policy.py without gate approval
    malicious_action = MockAction({
        "command": "str_replace",
        "path": "src/cooling_policy.py",
        "old_str": "return min(self.failsafe_limit, 85.0)",
        "new_str": "return 100.0 # Bypassed governor limit",
    })

    # Assert prohibited action is denied and raises ChronosSecurityVetoError
    with pytest.raises(ChronosSecurityVetoError) as exc_info:
        gw(malicious_action)

    assert "requires human gate approval" in str(exc_info.value)

    # Assert underlying tool was NEVER invoked
    assert len(underlying_tool.invocations) == 0

    # Assert disk state unchanged
    assert policy_file.read_text(encoding="utf-8") == COOLING_POLICY_CODE

    # Assert denial is audited correctly in hash-chained audit log
    valid, _, count = verify_audit_chain(audit_file)
    assert valid is True
    assert count == 1
    log_content = audit_file.read_text(encoding="utf-8")
    assert '"status": "deny"' in log_content
    assert "cooling_policy.py" in log_content

    # 2. Equivalent authorized action: admin grants gate approval for legitimate feature
    grant_gate_approval(con, agent_id=agent_id, path_pattern="src/cooling_policy.py", approved_by="admin")

    # Now equivalent authorized action succeeds
    authorized_action = MockAction({
        "command": "str_replace",
        "path": "src/cooling_policy.py",
        "old_str": "self.target_fan_speed = 30.0",
        "new_str": "self.target_fan_speed = 32.0 # Hysteresis feature",
    })
    res = gw(authorized_action)
    assert res["status"] == "success"
    assert len(underlying_tool.invocations) == 1

def test_p0_decouple_retrieval_from_lease_authorization(tmp_path):
    """Prove that AST retrieval indexes symbols but refuses to grant write leases on gated files without approval."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "cooling_policy.py").write_text(COOLING_POLICY_CODE, encoding="utf-8")

    db_file = repo_dir / "chronos.db"
    con = get_connection(db_file)

    agent_id = "agent_unauthorized"
    set_permissions(con, agent_id, {"allowed_paths": ["**"]})

    bootstrap = ChronosSessionBootstrap(repo_path=repo_dir, con=con)
    res = bootstrap.bootstrap_session(
        task_prompt="Override governor in src/cooling_policy.py",
        target_files=["src/cooling_policy.py"],
        agent_id=agent_id,
        session_id="session_test",
    )

    # AST context retrieval succeeded
    assert "CoolingPolicyEngine" in res["augmented_prompt"]

    # BUT write leases were NOT acquired on gated file!
    assert len(res["leases"]) == 0
    active_locks = con.execute("SELECT * FROM intent_locks WHERE agent_id = ?", (agent_id,)).fetchall()
    assert len(active_locks) == 0

# ---------------------------------------------------------------------------
# P1: AST Context Scoping & Targeted Caller Pruning Tests
# ---------------------------------------------------------------------------

def test_p1_targeted_context_scoping_prunes_distant_callers(tmp_path):
    """Prove that targeted tasks prune transitive callers to max 2 and label them Reference Only."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "normalizer.py").write_text("""
def normalize_plate(text):
    return text.upper().strip()
""", encoding="utf-8")

    (repo_dir / "caller1.py").write_text("""
from normalizer import normalize_plate
def svc1(): normalize_plate('x')
""", encoding="utf-8")

    (repo_dir / "caller2.py").write_text("""
from normalizer import normalize_plate
def svc2(): normalize_plate('y')
""", encoding="utf-8")

    (repo_dir / "caller3.py").write_text("""
from normalizer import normalize_plate
def svc3(): normalize_plate('z')
""", encoding="utf-8")

    nodes, edges = index_repository(repo_dir)
    con = get_connection(repo_dir / "chronos.db")
    store = SQLiteGraphStore(con)
    store.ingest(nodes, edges)

    # 1. Targeted context: is_targeted=True
    targeted_ctx = assemble_context(
        symbols=["normalize_plate"],
        graph_store=store,
        token_budget=2000,
        is_targeted=True,
        target_files=["normalizer.py"],
    )

    text = targeted_ctx["text"]
    assert "Reference Only — Do Not Modify" in text
    assert "Focused Task Scope" in text
    assert "Blast Radius / Impact" not in text

    # 2. Exploratory zero-path context: is_targeted=False
    exploratory_ctx = assemble_context(
        symbols=["normalize_plate"],
        graph_store=store,
        token_budget=2000,
        is_targeted=False,
    )
    assert "Blast Radius / Impact" in exploratory_ctx["text"]

# ---------------------------------------------------------------------------
# P2: Incremental Bi-Temporal Graph Synchronization Tests
# ---------------------------------------------------------------------------

def test_p2_incremental_graph_delta_updates(tmp_path):
    """Prove that modifying a file updates symbol nodes and bi-temporal edges without full repo re-indexing."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    calc_file = repo_dir / "calc.py"
    calc_file.write_text("""
def add(a, b):
    return a + b

def run_calc():
    return add(1, 2)
""", encoding="utf-8")

    con = get_connection(repo_dir / "chronos.db")
    store = SQLiteGraphStore(con)
    nodes, edges = index_repository(repo_dir)
    store.ingest(nodes, edges)

    # Initial state: run_calc calls add
    callers = store.as_of_callers("add")
    assert any(c["name"] == "run_calc" for c in callers)

    # Simulate edit: add 'multiply', remove 'add' from run_calc
    calc_file.write_text("""
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b

def run_calc():
    return multiply(3, 4)
""", encoding="utf-8")

    # Trigger incremental file update
    num_nodes, num_edges = store.update_file(calc_file)
    assert num_nodes >= 3

    # As-of latest: run_calc now calls multiply, NOT add
    callees = store.as_of_callees("run_calc")
    assert any(c["name"] == "multiply" for c in callees)
    assert not any(c["name"] == "add" for c in callees)
