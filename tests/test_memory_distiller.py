"""Unit tests for Chronos knowledge distillation and post-task hooks."""

import sqlite3
import pytest
from pathlib import Path

from chronos.audit.logger import verify_audit_chain
from chronos.core.db import get_connection
from chronos.memory.distiller import distill_task_memories
from chronos.memory.hooks import on_task_completed
from chronos.memory.models import (
    MemoryTier,
    OutcomeType,
    TaskCompletionPayload,
    VerificationStatus,
)
from chronos.memory.store import ChronosMemoryStore


def test_distill_successful_task():
    payload = TaskCompletionPayload(
        task_id="task_101",
        agent_id="test_agent",
        repo_namespace="chronos",
        task_prompt="Fix null pointer exception when indexing empty go packages",
        outcome=OutcomeType.SUCCESS,
        summary="Fixed nil package crash in Go parser",
        files_modified=["chronos/context/indexer.py"],
        test_results={"passed": True, "failed": 0, "returncode": 0},
        git_diff="""
diff --git a/chronos/context/indexer.py b/chronos/context/indexer.py
--- a/chronos/context/indexer.py
+++ b/chronos/context/indexer.py
+    if package is None:
+        return []
""",
        conversation_messages=[
            {"role": "assistant", "content": "The bug was caused by missing nil check on package declaration."},
            {"role": "assistant", "content": "Always ensure Go package declarations are guarded against nil nodes."},
        ],
    )

    records = distill_task_memories(payload)
    assert len(records) >= 3

    # Check episodic record
    episodic = [r for r in records if r.tier == MemoryTier.EPISODIC][0]
    assert episodic.task_id == "task_101"
    assert episodic.verification_status == VerificationStatus.VERIFIED
    assert "indexer.py" in episodic.content

    # Check learned bug fix record
    learned = [r for r in records if r.tier == MemoryTier.LEARNED and "missing nil check" in r.content][0]
    assert learned.verification_status == VerificationStatus.VERIFIED
    assert learned.confidence >= 0.85

    # Check invariant record
    invariant = [r for r in records if "guarded against nil" in r.content][0]
    assert invariant.tier == MemoryTier.PROCEDURAL
    assert invariant.verification_status == VerificationStatus.VERIFIED


def test_distill_failed_task_and_hypothesis():
    payload = TaskCompletionPayload(
        task_id="task_102",
        agent_id="test_agent",
        repo_namespace="chronos",
        task_prompt="Attempting to replace SQLite with Redis for locks",
        outcome=OutcomeType.FAILURE,
        summary="Redis backend failed connection tests",
        files_modified=["chronos/coordination/ledger.py"],
        test_results={"passed": False, "failed": 3, "stderr": "ConnectionRefusedError: [Errno 111]"},
        error_message="Redis server was unavailable in local offline mode",
    )

    records = distill_task_memories(payload)
    failed_hypo = [r for r in records if r.outcome == OutcomeType.FAILED_HYPOTHESIS][0]
    assert failed_hypo.verification_status == VerificationStatus.FAILED
    assert "Failed approach" in failed_hypo.content
    assert "Redis server was unavailable" in failed_hypo.content


def test_idempotent_post_task_hook(tmp_path: Path):
    db_file = tmp_path / "chronos.db"
    log_file = tmp_path / "audit.jsonl"
    con = get_connection(db_file)

    # First execution of post-task hook
    ids_1 = on_task_completed(
        con=con,
        task_id="task_201",
        task_prompt="Optimize SQLite FTS5 query tokenization",
        agent_id="agent_alpha",
        repo_namespace="chronos",
        outcome=OutcomeType.SUCCESS,
        summary="Added prefix token matching for terms > 3 chars",
        files_modified=["chronos/memory/store.py"],
        test_results={"passed": True},
        audit_log_path=log_file,
    )
    assert len(ids_1) > 0

    # Verify audit event was appended
    is_valid, msg, total_events = verify_audit_chain(log_file)
    assert is_valid is True
    assert total_events == 1

    # Second execution with exact same task_id should be a no-op (idempotent)
    ids_2 = on_task_completed(
        con=con,
        task_id="task_201",
        task_prompt="Optimize SQLite FTS5 query tokenization",
        agent_id="agent_alpha",
        repo_namespace="chronos",
        outcome=OutcomeType.SUCCESS,
        audit_log_path=log_file,
    )
    assert ids_2 == []

    # Verify count of memories in DB for task_201 didn't duplicate
    count = con.execute("SELECT count(*) FROM memories WHERE task_id = 'task_201'").fetchone()[0]
    assert count == len(ids_1)
    con.close()
