"""Tests for ChronosSessionBootstrap automatic knowledge injection (Phase 2)."""

from pathlib import Path
import sqlite3
import pytest

from chronos.core.db import get_connection
from chronos.verification.rule_store import upsert_rule, promote_rule
from runtime.session import ChronosSessionBootstrap

SAMPLE_CODE = """
def calculate_interest(principal, rate, time):
    return principal * rate * time

def generate_statement(user_id, account_id):
    interest = calculate_interest(1000, 0.05, 1)
    return f"User {user_id}: Statement interest {interest}"
"""

def test_bootstrap_session_generates_context_envelope(tmp_path):
    repo_dir = tmp_path / "sample_repo"
    repo_dir.mkdir()
    src_dir = repo_dir / "src"
    src_dir.mkdir()
    target_file = src_dir / "finance.py"
    target_file.write_text(SAMPLE_CODE, encoding="utf-8")

    db_path = tmp_path / "chronos.db"
    con = get_connection(db_path)

    boot = ChronosSessionBootstrap(repo_path=repo_dir, con=con)
    res = boot.bootstrap_session(
        task_prompt="Fix interest compounding logic",
        target_files=["src/finance.py"],
        agent_id="test_agent_1",
        token_budget=1500,
    )

    assert "envelope_text" in res
    assert "augmented_prompt" in res
    assert "Fix interest compounding logic" in res["augmented_prompt"]
    assert "Chronos Repository Context" in res["envelope_text"]
    assert "calculate_interest" in res["symbols"]
    assert res["token_count"] <= 1500
    assert len(res["leases"]) >= 1

    con.close()

def test_bootstrap_session_includes_active_rules(tmp_path):
    repo_dir = tmp_path / "rule_repo"
    repo_dir.mkdir()
    (repo_dir / "calc.py").write_text("def run(): pass\n", encoding="utf-8")

    db_path = tmp_path / "chronos.db"
    con = get_connection(db_path)

    upsert_rule(con, "sec-001", "python", "No os.system calls", yaml_pattern=r"os\.system\(", detectability_passed=True)
    promote_rule(con, "sec-001", "sec_lead")

    boot = ChronosSessionBootstrap(repo_path=repo_dir, con=con)
    res = boot.bootstrap_session(
        task_prompt="Implement helper function",
        target_files=["calc.py"],
        agent_id="test_agent_2",
    )

    assert "sec-001" in res["envelope_text"]
    assert "No os.system" in res["envelope_text"] or r"os\.system\(" in res["envelope_text"]
    con.close()
