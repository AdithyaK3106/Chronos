"""End-to-end lifecycle integration tests for Chronos Epistemic Living Memory."""

import sqlite3
import pytest
from pathlib import Path

from chronos.core.db import get_connection
from chronos.memory.hooks import on_task_completed
from chronos.memory.models import OutcomeType, VerificationStatus
from chronos.memory.store import ChronosMemoryStore
from runtime.session import ChronosSessionBootstrap


def test_cross_session_memory_injection_lifecycle(tmp_path: Path):
    """Verify closed-loop learning:

    Task 1 finishes -> hook distills knowledge -> Task 2 bootstraps -> injects lesson.
    """
    repo_dir = tmp_path / "my_project"
    repo_dir.mkdir(parents=True)
    (repo_dir / "calc.py").write_text("def divide(a, b):\n    return a / b\n")

    db_file = repo_dir / ".chronos" / "chronos.db"
    log_file = repo_dir / ".chronos" / "chronos-audit.log"
    con = get_connection(db_file)

    # -------------------------------------------------------------
    # Session 1: Agent fixes division by zero bug
    # -------------------------------------------------------------
    saved_ids = on_task_completed(
        con=con,
        task_id="task_calc_01",
        task_prompt="Fix ZeroDivisionError when dividing by zero in calc.py",
        agent_id="agent_1",
        repo_namespace="my_project",
        outcome=OutcomeType.SUCCESS,
        summary="Added ZeroDivisionError guard in divide function",
        files_modified=["calc.py"],
        test_results={"passed": True, "failed": 0},
        git_diff="""
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,4 @@
 def divide(a, b):
+    if b == 0:
+        raise ValueError("Cannot divide by zero")
     return a / b
""",
        conversation_messages=[
            {"role": "assistant", "content": "The bug was caused by missing denominator zero validation."},
            {"role": "assistant", "content": "Always ensure arithmetic denominator is checked for zero before division."},
        ],
        audit_log_path=log_file,
    )
    assert len(saved_ids) >= 2
    con.close()

    # -------------------------------------------------------------
    # Session 2: A different agent in a fresh session on related task
    # -------------------------------------------------------------
    con2 = get_connection(db_file)
    bootstrap = ChronosSessionBootstrap(repo_path=repo_dir, con=con2)

    res = bootstrap.bootstrap_session(
        task_prompt="Implement safe division helper for floating point numbers in calc.py",
        agent_id="agent_2",
    )

    # Verify memory was retrieved and injected into pre-turn-1 envelope
    assert res["memories_count"] > 0
    envelope = res["envelope_text"]
    assert "Chronos Repository Memory & Past Postmortems" in envelope
    assert "missing denominator zero validation" in envelope or "denominator is checked for zero" in envelope
    assert "Advisory Only" in envelope

    # -------------------------------------------------------------
    # Session 3: An unrelated task in the same repo should NOT inject math gotcha
    # -------------------------------------------------------------
    res_unrelated = bootstrap.bootstrap_session(
        task_prompt="Configure HTTP server logging with JSON formatter",
        agent_id="agent_3",
    )
    # The math division memory should not score high enough to appear
    assert "ZeroDivisionError" not in res_unrelated["envelope_text"]
    con2.close()


def test_cross_repository_namespace_isolation_in_bootstrap(tmp_path: Path):
    """Verify that memories from repo A are not injected when running in repo B."""
    repo_a = tmp_path / "repo_a"
    repo_a.mkdir(parents=True)
    db_file_a = repo_a / ".chronos" / "chronos.db"
    con_a = get_connection(db_file_a)

    # Store memory in repo A
    on_task_completed(
        con=con_a,
        task_id="task_a_01",
        task_prompt="Configure OAuth2 token exchange",
        agent_id="agent_a",
        repo_namespace="repo_a",
        outcome=OutcomeType.SUCCESS,
        summary="Set up OAuth2 token exchange with identity provider",
        conversation_messages=[
            {"role": "assistant", "content": "The bug was caused by invalid OAuth2 audience claim in token header."},
        ],
    )
    con_a.close()

    # Bootstrap in repo B (with a separate database)
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir(parents=True)
    db_file_b = repo_b / ".chronos" / "chronos.db"
    con_b = get_connection(db_file_b)

    bootstrap_b = ChronosSessionBootstrap(repo_path=repo_b, con=con_b)
    res_b = bootstrap_b.bootstrap_session(
        task_prompt="Fix OAuth2 token validation error",
        agent_id="agent_b",
    )

    # Repo B has no memories recorded, so count should be 0
    assert res_b["memories_count"] == 0
    assert "OAuth2 audience claim" not in res_b["envelope_text"]
    con_b.close()
