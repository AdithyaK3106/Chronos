"""Integration tests for Turn 1 Zero-Path Context Envelope Delivery in OpenHands."""

from pathlib import Path
import pytest

from openhands.sdk import Tool
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.testing import TestLLM
from openhands.tools.file_editor import FileEditorTool

from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.security.permissions import set_permissions
from runtime.openhands_adapter import ChronosAgentRuntime

SAMPLE_SERVICE_CODE = '''"""Calculator math service module."""

def compute_sum(values: list[float]) -> float:
    """Compute arithmetic sum of numerical values."""
    total = 0.0
    for v in values:
        total += v
    return total

def process_batch_totals(batches: list[list[float]]) -> list[float]:
    """Process multiple batches using compute_sum."""
    results = []
    for b in batches:
        results.append(compute_sum(b))
    return results
'''

SAMPLE_TEST_CODE = '''"""Unit tests for calculator service."""
from src.calc import compute_sum

def test_compute_sum():
    assert compute_sum([1.0, 2.0, 3.0]) == 6.0
'''


def test_first_turn_zero_path_injection_without_target_files(tmp_path):
    """Prove that natural-language prompt without paths automatically receives AST context on Turn 1."""
    repo_dir = tmp_path / "calc_service"
    repo_dir.mkdir()
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()

    (src_dir / "calc.py").write_text(SAMPLE_SERVICE_CODE, encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(SAMPLE_TEST_CODE, encoding="utf-8")

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

    agent_id = "zero_path_agent"
    set_permissions(
        con,
        agent_id=agent_id,
        manifest={
            "allowed_paths": ["src/**", "tests/**"],
            "denied_paths": [".*"],
            "read_only": False,
            "allowed_tools": ["file_editor"],
        },
    )

    runtime = ChronosAgentRuntime(config=cfg, db_con=con)

    test_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="Received zero-path context envelope. Processing compute_sum fix.")]),
    ])
    agent = runtime.create_agent(llm=test_llm, agent_id=agent_id, tools=[Tool(name=FileEditorTool.name)])

    # Submit natural-language task prompt with ZERO file paths specified
    prompt = "Fix the bug in compute_sum where numerical values produce incorrect total calculation"
    conversation, telemetry = runtime.run_task(
        agent=agent,
        agent_id=agent_id,
        task_prompt=prompt,
        target_files=None,  # Pure zero-path resolution
    )

    # 1. Verify telemetry completed
    assert telemetry.end_time is not None

    # 2. Inspect first conversation message delivered to the agent
    events = conversation.state.active_branch()
    user_events = [e for e in events if hasattr(e, "llm_message") and getattr(e.llm_message, "role", None) == "user"]
    assert len(user_events) >= 1
    first_message = user_events[0].llm_message
    msg_text = first_message.content[0].text if hasattr(first_message.content[0], "text") else str(first_message.content[0])

    # a. Original prompt is preserved
    assert prompt in msg_text

    # b. Resolved AST context envelope is present
    assert "Chronos Repository Context" in msg_text or "Retrieval Provenance" in msg_text
    assert "compute_sum" in msg_text

    # c. Provenance rationale is included
    assert "Retrieval Provenance & Relevance Rationale" in msg_text
    assert "calc.py" in msg_text

    # d. Associated test files discovered
    assert "test_calc.py" in msg_text

    con.close()


def test_first_turn_injection_no_match_graceful_handling(tmp_path):
    """Prove that unrelated queries without code matches still produce a valid, safe message envelope."""
    repo_dir = tmp_path / "empty_repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text("def ping(): return 'pong'\n", encoding="utf-8")

    chronos_dir = repo_dir / ".chronos"
    chronos_dir.mkdir()
    db_file = chronos_dir / "chronos.db"

    con = get_connection(db_file)
    cfg = ChronosConfig(
        repo_path=str(repo_dir),
        db_path=str(db_file),
        audit_log_path=str(chronos_dir / "audit.log"),
    )

    runtime = ChronosAgentRuntime(config=cfg, db_con=con)
    agent_id = "test_agent"
    set_permissions(con, agent_id=agent_id, manifest={"allowed_paths": ["**"]})

    test_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="I cannot help bake sourdough bread.")]),
    ])
    agent = runtime.create_agent(llm=test_llm, agent_id=agent_id, tools=[Tool(name=FileEditorTool.name)])

    prompt = "How do I bake sourdough bread with whole wheat flour?"
    conversation, _ = runtime.run_task(
        agent=agent,
        agent_id=agent_id,
        task_prompt=prompt,
    )

    events = conversation.state.active_branch()
    user_events = [e for e in events if hasattr(e, "llm_message") and getattr(e.llm_message, "role", None) == "user"]
    assert len(user_events) >= 1
    first_message = user_events[0].llm_message
    msg_text = first_message.content[0].text if hasattr(first_message.content[0], "text") else str(first_message.content[0])

    assert prompt in msg_text
    assert "No high-confidence symbols matched" in msg_text
    con.close()
