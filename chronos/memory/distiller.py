"""Safe, deterministic knowledge distillation for Chronos tasks.

Extracts concise, evidence-backed memories from completed task metadata,
git diffs, and verification test outputs with zero external LLM dependencies.
All inputs are treated as untrusted and scrubbed of credentials before persistence.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from chronos.memory.models import (
    MemoryRecord,
    MemoryTier,
    OutcomeType,
    TaskCompletionPayload,
    VerificationStatus,
)
from chronos.security.sanitizer import redact_secrets


def _truncate_evidence(text: str, max_chars: int = 500) -> str:
    """Keep evidence concise and avoid raw context dumps."""
    clean = text.strip()
    if len(clean) <= max_chars:
        return clean
    return clean[:max_chars] + "\n...(truncated for memory compactness)"


def distill_task_memories(payload: TaskCompletionPayload) -> List[MemoryRecord]:
    """Deterministically extract tiered memories from task completion payload."""
    memories: List[MemoryRecord] = []
    seen_contents = set()

    # Determine verification status from test results
    tests_passed = False
    tests_failed = False
    if payload.test_results:
        # Check standard test keys: passed, returncode == 0, failed == 0
        if payload.test_results.get("passed") is True or payload.test_results.get("returncode") == 0:
            tests_passed = True
        elif payload.test_results.get("passed") is False or (payload.test_results.get("failed") or 0) > 0:
            tests_failed = True

    base_verification = (
        VerificationStatus.VERIFIED
        if tests_passed
        else (VerificationStatus.FAILED if tests_failed else VerificationStatus.UNVERIFIED)
    )

    clean_prompt = redact_secrets(payload.task_prompt.strip())
    clean_diff = redact_secrets(payload.git_diff.strip())
    clean_error = redact_secrets(payload.error_message.strip()) if payload.error_message else ""

    # -------------------------------------------------------------
    # 1. Episodic Task Memory (Task execution summary)
    # -------------------------------------------------------------
    files_str = ", ".join(payload.files_modified[:5]) if payload.files_modified else "no files modified"
    task_desc = payload.summary or clean_prompt.split("\n")[0][:140]
    
    episodic_content = f"Task [{payload.task_id}] '{task_desc}': Outcome={payload.outcome.value}. Touched: {files_str}."
    if episodic_content not in seen_contents:
        seen_contents.add(episodic_content)
        evidence_text = ""
        if clean_diff:
            evidence_text = f"Diff excerpt:\n{_truncate_evidence(clean_diff, 400)}"
        elif clean_error:
            evidence_text = f"Error excerpt:\n{_truncate_evidence(clean_error, 300)}"

        memories.append(
            MemoryRecord(
                repo_namespace=payload.repo_namespace,
                tier=MemoryTier.EPISODIC,
                content=episodic_content,
                summary=f"Task {payload.task_id} execution ({payload.outcome.value})",
                source="post_task_hook",
                task_id=payload.task_id,
                agent_id=payload.agent_id,
                outcome=payload.outcome,
                verification_status=base_verification,
                confidence=1.0,
                importance=0.4,
                evidence=evidence_text,
                tags=["task_run", payload.outcome.value] + [Path(f).name for f in payload.files_modified[:3] if f],
                metadata={
                    "files_modified": payload.files_modified,
                    "tests_passed": tests_passed,
                },
            )
        )

    # -------------------------------------------------------------
    # 2. Failed Hypothesis Recording
    # -------------------------------------------------------------
    if payload.outcome in (OutcomeType.FAILURE, OutcomeType.FAILED_HYPOTHESIS) or tests_failed:
        fail_reason = clean_error or (payload.test_results.get("stderr") or "Tests failed during verification")
        hypothesis_content = (
            f"Failed approach in [{files_str}]: Attempt to resolve '{clean_prompt[:100]}' "
            f"resulted in failure: {_truncate_evidence(fail_reason, 160)}. Avoid repeating this pattern."
        )
        if hypothesis_content not in seen_contents:
            seen_contents.add(hypothesis_content)
            memories.append(
                MemoryRecord(
                    repo_namespace=payload.repo_namespace,
                    tier=MemoryTier.LEARNED,
                    content=hypothesis_content,
                    summary=f"Failed hypothesis on {files_str}",
                    source="post_task_hook",
                    task_id=payload.task_id,
                    agent_id=payload.agent_id,
                    outcome=OutcomeType.FAILED_HYPOTHESIS,
                    verification_status=VerificationStatus.FAILED,
                    confidence=0.9,
                    importance=0.75,
                    evidence=_truncate_evidence(fail_reason, 400),
                    tags=["failed_hypothesis", "regression_risk"],
                    metadata={"error": _truncate_evidence(clean_error, 200)},
                )
            )

    # -------------------------------------------------------------
    # 3. Learning & Bug Fix Extraction (from conversation & diffs)
    # -------------------------------------------------------------
    for msg in payload.conversation_messages:
        text = msg.get("content", "")
        if not isinstance(text, str) or len(text) < 15:
            continue

        clean_text = redact_secrets(text)

        # Bug root causes and fixes
        root_cause_matches = re.findall(
            r"(?:the bug was caused by|root cause was|fixed by|turns out that|discovered that)\s+([^.\n]+[\.\n])",
            clean_text,
            flags=re.IGNORECASE,
        )
        for rc in root_cause_matches:
            finding = rc.strip()
            if len(finding) > 15 and finding not in seen_contents:
                seen_contents.add(finding)
                memories.append(
                    MemoryRecord(
                        repo_namespace=payload.repo_namespace,
                        tier=MemoryTier.LEARNED,
                        content=f"Learned resolution: {finding}",
                        summary=f"Bug fix finding: {finding[:60]}",
                        source="post_task_hook",
                        task_id=payload.task_id,
                        agent_id=payload.agent_id,
                        outcome=payload.outcome,
                        verification_status=base_verification,
                        confidence=0.85 if tests_passed else 0.6,
                        importance=0.8 if tests_passed else 0.5,
                        evidence=_truncate_evidence(clean_diff, 300) if clean_diff else "",
                        tags=["learned_fix", "bug_root_cause"],
                    )
                )

        # Procedural / Invariant rules
        rule_matches = re.findall(
            r"(?:always ensure|never modify|invariant:|must always|rule:)\s+([^.\n]+[\.\n])",
            clean_text,
            flags=re.IGNORECASE,
        )
        for r_match in rule_matches:
            rule_text = r_match.strip()
            if len(rule_text) > 15 and rule_text not in seen_contents:
                seen_contents.add(rule_text)
                # Invariants are only core if tests actually verified them
                tier = MemoryTier.PROCEDURAL if tests_passed else MemoryTier.LEARNED
                memories.append(
                    MemoryRecord(
                        repo_namespace=payload.repo_namespace,
                        tier=tier,
                        content=f"Repository invariant: {rule_text}",
                        summary=f"Invariant: {rule_text[:60]}",
                        source="post_task_hook",
                        task_id=payload.task_id,
                        agent_id=payload.agent_id,
                        outcome=payload.outcome,
                        verification_status=base_verification,
                        confidence=0.9 if tests_passed else 0.5,
                        importance=0.85,
                        tags=["rule", "invariant"],
                    )
                )

    return memories
