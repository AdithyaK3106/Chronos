"""Data models for Chronos Epistemic Living Memory."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class MemoryTier(str, Enum):
    """Memory retention and authority tier."""
    CORE = "core"              # Invariants, fundamental system rules, architecture
    LEARNED = "learned"        # Bug root causes, gotchas, framework quirks, edge cases
    EPISODIC = "episodic"      # Event history, past task narratives, tools used
    WORKING = "working"        # Ephemeral task state, TODOs (24h TTL)
    PROCEDURAL = "procedural"  # Behavioral guidelines, formatting rules, testing procedures


class OutcomeType(str, Enum):
    """Task outcome classification."""
    SUCCESS = "success"
    FAILURE = "failure"
    FAILED_HYPOTHESIS = "failed_hypothesis"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    """Epistemic verification status."""
    VERIFIED = "verified"      # Backed by passing test suite or deterministic check
    UNVERIFIED = "unverified"  # Heuristic extraction or agent assertion
    FAILED = "failed"          # Backed by failing test suite or rejected hypothesis


class MemoryRecord(BaseModel):
    """A persistent epistemic memory entry."""
    id: Optional[int] = None
    repo_namespace: str = Field(default="", description="Repository identifier or directory name")
    tier: MemoryTier = Field(default=MemoryTier.LEARNED)
    content: str = Field(..., description="The distilled knowledge, invariant, or gotcha")
    summary: str = Field(default="", description="Concise 1-line summary")
    source: str = Field(default="manual", description="Origin: post_task_hook, manual, conversation")
    task_id: str = Field(default="", description="ID of task where this knowledge was extracted")
    agent_id: str = Field(default="", description="Agent identity that generated/verified the lesson")
    outcome: OutcomeType = Field(default=OutcomeType.SUCCESS)
    verification_status: VerificationStatus = Field(default=VerificationStatus.UNVERIFIED)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: str = Field(default="", description="Scrubbed git diff excerpt or test failure message")
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    supersedes: Optional[int] = Field(default=None, description="ID of superseded memory version")
    archived: bool = Field(default=False)
    created_at: str = Field(default_factory=_utc_now_iso)
    updated_at: str = Field(default_factory=_utc_now_iso)


class RecallResult(BaseModel):
    """A scored result returned from memory search."""
    id: int
    content: str
    summary: str = ""
    tier: MemoryTier
    source: str = ""
    repo_namespace: str = ""
    score: float
    method: str = "fts"  # fts, vector, hybrid
    importance: float = 0.5
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED
    outcome: OutcomeType = OutcomeType.SUCCESS
    evidence: str = ""
    tags: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class TaskCompletionPayload(BaseModel):
    """Payload delivered to the post-task distillation hook."""
    task_id: str
    agent_id: str = "default_agent"
    repo_namespace: str = ""
    task_prompt: str
    outcome: OutcomeType = OutcomeType.SUCCESS
    summary: str = ""
    files_modified: List[str] = Field(default_factory=list)
    test_results: Dict[str, Any] = Field(default_factory=dict)
    conversation_messages: List[Dict[str, Any]] = Field(default_factory=list)
    git_diff: str = ""
    error_message: Optional[str] = None


class MemoryStats(BaseModel):
    """Aggregated memory store statistics."""
    total_memories: int
    by_tier: Dict[str, int]
    by_outcome: Dict[str, int]
    archived_count: int
    namespaces: List[str]
