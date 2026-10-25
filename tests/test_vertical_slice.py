"""Vertical Slice Integration Test: OpenHands SDK Agent with Chronos Governance."""

from pathlib import Path
import pytest
from openhands.sdk.testing import TestLLM
from openhands.sdk.llm import Message, TextContent, MessageToolCall
from openhands.sdk import Tool
from openhands.tools.file_editor import FileEditorTool

from chronos.audit.logger import verify_audit_chain
from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.security.gates import grant_gate_approval
from chronos.security.permissions import set_permissions
from runtime.openhands_adapter import ChronosAgentRuntime

SAMPLE_REPO_CODE = """
def authenticate_user(username, password):
    return username == 'admin'

def login_handler(request):
    return authenticate_user(request['user'], request['pass'])
"""

def test_runnable_vertical_slice(tmp_path):
    # 1. Setup repository layout
    repo_dir = tmp_path / "target_repo"
    repo_dir.mkdir()
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    (src_dir / "auth.py").write_text(SAMPLE_REPO_CODE, encoding="utf-8")
    
    chronos_dir = repo_dir / ".chronos"
    chronos_dir.mkdir()
    db_file = chronos_dir / "chronos.db"
    audit_file = chronos_dir / "chronos-audit.log"

    con = get_connection(db_file)
    cfg = ChronosConfig(
        repo_path=str(repo_dir),
        db_path=str(db_file),
        audit_log_path=str(audit_file),
    )

    # 2. Index repository AST into bi-temporal graph
    nodes, edges = index_repository(repo_dir)
    store = SQLiteGraphStore(con)
    store.ingest(nodes, edges)

    # Verify indexing captured symbols
    callers = store.as_of_callers("authenticate_user")
    assert len(callers) >= 1
    assert any(c["name"] == "login_handler" for c in callers)

    # 3. Assemble context under token budget
    ctx = assemble_context(["authenticate_user"], store, token_budget=1000)
    assert "authenticate_user" in ctx["text"]
    assert ctx["token_count"] <= 1000

    # 4. Initialize Chronos Agent Runtime
    runtime = ChronosAgentRuntime(config=cfg, db_con=con, graph_store=store)
    agent_id = "agent_coder_v1"

    set_permissions(
        con,
        agent_id=agent_id,
        manifest={
            "allowed_paths": ["src/**"],
            "denied_paths": ["secrets/**", ".*"],
            "read_only": False,
            "allowed_tools": ["file_editor"],
        },
    )
    grant_gate_approval(con, agent_id=agent_id, path_pattern="src/auth.py", approved_by="admin")

    # 5. Create OpenHands Agent with TestLLM (zero paid API keys required)
    test_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="Inspected repository. Ready to modify.")]),
    ])
    agent = runtime.create_agent(llm=test_llm, agent_id=agent_id, tools=[Tool(name=FileEditorTool.name)])
    assert agent is not None

    # 6. Test permitted tool action interception
    allowed, reason1 = runtime.intercept_and_evaluate_action(
        agent_id=agent_id,
        tool_name="file_editor",
        tool_input={"path": "src/auth.py", "content": "# Updated comment\n" + SAMPLE_REPO_CODE},
    )
    assert allowed is True
    assert reason1 == "Allowed"

    # 7. Test deliberately unauthorized tool action interception (path traversal)
    denied, reason2 = runtime.intercept_and_evaluate_action(
        agent_id=agent_id,
        tool_name="file_editor",
        tool_input={"path": "src/../../etc/passwd"},
    )
    assert denied is False
    assert "escapes repository root" in reason2

    # 8. Test unauthorized tool action (denied path pattern)
    denied_secret, reason3 = runtime.intercept_and_evaluate_action(
        agent_id=agent_id,
        tool_name="file_editor",
        tool_input={"path": "secrets/token.env"},
    )
    assert denied_secret is False
    assert "restricted pattern" in reason3 or "not in allowed_paths" in reason3

    # 9. Verify tamper-evident audit log has recorded the decisions
    is_valid, msg, event_count = verify_audit_chain(audit_file)
    assert is_valid is True
    assert event_count >= 3, f"Expected at least 3 audit entries, got {event_count}"

    con.close()
