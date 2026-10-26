"""Post-task lifecycle hooks for Chronos Epistemic Living Memory."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from chronos.audit.logger import append_audit_event
from chronos.memory.distiller import distill_task_memories
from chronos.memory.models import OutcomeType, TaskCompletionPayload
from chronos.memory.store import ChronosMemoryStore

logger = logging.getLogger(__name__)


def on_task_completed(
    con: sqlite3.Connection,
    task_id: str,
    task_prompt: str,
    agent_id: str = "default_agent",
    repo_namespace: str = "",
    outcome: Union[OutcomeType, str] = OutcomeType.SUCCESS,
    summary: str = "",
    files_modified: Optional[List[str]] = None,
    test_results: Optional[Dict[str, Any]] = None,
    conversation_messages: Optional[List[Dict[str, Any]]] = None,
    git_diff: str = "",
    error_message: Optional[str] = None,
    audit_log_path: Optional[Union[str, Path]] = None,
) -> List[int]:
    """Idempotent post-task hook called when an agent concludes task execution.

    Distills memories, persists them transactionally, and records an audit event.
    Guarantees:
    - Idempotent: repeated calls for the same task_id do not duplicate memories.
    - Resilient: errors never mask the task's primary execution status.
    """
    if not task_id:
        return []

    try:
        # Idempotency check: verify if post-task memory for this task_id was already persisted
        row = con.execute(
            "SELECT id FROM memories WHERE task_id = ? AND source = 'post_task_hook' LIMIT 1",
            (task_id,),
        ).fetchone()
        if row:
            logger.debug(f"Task '{task_id}' already has recorded memories. Skipping (idempotent).")
            return []

        payload = TaskCompletionPayload(
            task_id=task_id,
            agent_id=agent_id,
            repo_namespace=repo_namespace,
            task_prompt=task_prompt,
            outcome=OutcomeType(outcome),
            summary=summary,
            files_modified=files_modified or [],
            test_results=test_results or {},
            conversation_messages=conversation_messages or [],
            git_diff=git_diff,
            error_message=error_message,
        )

        # 1. Distill candidate memories
        records = distill_task_memories(payload)
        if not records:
            return []

        # 2. Persist transactionally
        store = ChronosMemoryStore(con=con)
        saved_ids = store.remember_batch(records, repo_namespace=repo_namespace)

        # 3. Log audit event
        if audit_log_path:
            try:
                append_audit_event(
                    log_path=audit_log_path,
                    event_type="memory_recorded",
                    agent_id=agent_id,
                    action="post_task_distillation",
                    target=task_id,
                    status="allow",
                    reason=f"Recorded {len(saved_ids)} memories for task outcome '{payload.outcome.value}'",
                )
            except Exception:
                pass

        return saved_ids

    except Exception as exc:
        logger.error(f"Post-task distillation hook encountered error: {exc}", exc_info=True)
        if audit_log_path:
            try:
                append_audit_event(
                    log_path=audit_log_path,
                    event_type="memory_recorded",
                    agent_id=agent_id,
                    action="post_task_distillation",
                    target=task_id,
                    status="error",
                    reason=f"Distillation failed: {exc}",
                )
            except Exception:
                pass
        # Never raise to avoid masking original task execution exceptions
        return []
