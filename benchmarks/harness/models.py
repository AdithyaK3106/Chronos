"""Universal Pydantic data schemas for chronos-bench."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class TaskSuiteType(str, Enum):
    """Evaluation suite type."""
    EFFICACY = "efficacy"
    SECURITY = "security"


class TaskCategory(str, Enum):
    """Categorization of coding or security task."""
    BUG_FIX = "bug_fix"
    FEATURE = "feature"
    REFACTOR = "refactor"
    SECURITY_CHALLENGE = "security_challenge"


class RunStatus(str, Enum):
    """Mutually exclusive execution outcome status."""
    SUCCESS = "SUCCESS"
    TASK_FAILURE = "TASK_FAILURE"
    REGRESSION_FAILURE = "REGRESSION_FAILURE"
    INVALID_PATCH_ERROR = "INVALID_PATCH_ERROR"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT_ERROR = "RATE_LIMIT_ERROR"
    MODEL_API_ERROR = "MODEL_API_ERROR"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    BLOCKED_PRE_EXECUTION = "BLOCKED_PRE_EXECUTION"  # Chronos correctly vetoed dangerous action
    SECURITY_BREACH = "SECURITY_BREACH"              # Unauthorized modification executed


class ArmConfiguration(str, Enum):
    """Experimental evaluation arm."""
    BASELINE = "baseline"
    TREATMENT = "treatment"


class RepositoryConfig(BaseModel):
    """Agnostic repository configuration."""
    id: str
    name: str
    path: str
    baseline_commit: Optional[str] = "HEAD"
    default_test_command: Optional[str] = None
    setup_commands: List[str] = Field(default_factory=list)


class BenchmarkTask(BaseModel):
    """Agnostic benchmark task specification."""
    id: str
    repository_id: str
    suite: TaskSuiteType = TaskSuiteType.EFFICACY
    category: TaskCategory = TaskCategory.BUG_FIX
    prompt: str
    starting_commit: Optional[str] = None
    target_files: List[str] = Field(default_factory=list)
    verification_commands: List[str] = Field(default_factory=list)
    regression_commands: List[str] = Field(default_factory=list)
    timeout_seconds: int = 300
    max_iterations: int = 15
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ModelProviderConfig(BaseModel):
    """Execution model provider configuration."""
    name: str = "agy"  # "agy", "litellm", "mock"
    model: str = "gemini-3.8-flash-medium"
    temperature: float = 0.0
    max_output_tokens: int = 4096
    timeout_seconds: int = 120
    rate_limit_backoff_seconds: float = 30.0
    max_retries: int = 3
    extra_params: Dict[str, Any] = Field(default_factory=dict)


class RunUsageMetadata(BaseModel):
    """Token and cost accounting metadata (null if unavailable)."""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cost_usd: Optional[float] = None


class TaskRunResult(BaseModel):
    """Result of an individual task run on one arm."""
    run_id: str
    task_id: str
    repository_id: str
    suite: TaskSuiteType
    arm: ArmConfiguration
    iteration: int
    status: RunStatus
    duration_seconds: float
    resolved_model_id: str
    provider_name: str
    usage: RunUsageMetadata = Field(default_factory=RunUsageMetadata)
    tool_calls_count: int = 0
    tool_call_types: Dict[str, int] = Field(default_factory=dict)
    patch_diff: str = ""
    stdout: str = ""
    stderr: str = ""
    error_message: Optional[str] = None
    policy_decisions: List[Dict[str, Any]] = Field(default_factory=list)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class PairedTrialResult(BaseModel):
    """Paired baseline and treatment result for an iteration."""
    task_id: str
    iteration: int
    baseline_run: Optional[TaskRunResult] = None
    treatment_run: Optional[TaskRunResult] = None


class StatisticalMetrics(BaseModel):
    """Aggregated paired metrics across multiple runs."""
    suite: TaskSuiteType
    total_tasks: int
    total_paired_runs: int
    baseline_success_count: int
    treatment_success_count: int
    baseline_success_rate: float
    treatment_success_rate: float
    delta_success_rate: float
    mcnemar_p_value: Optional[float] = None
    bootstrap_ci_95: Optional[List[float]] = None  # [lower, upper]
    median_duration_baseline_sec: float
    median_duration_treatment_sec: float
    duration_delta_sec: float
    mean_token_usage_baseline: Optional[float] = None
    mean_token_usage_treatment: Optional[float] = None
    token_reduction_pct: Optional[float] = None
    security_attempted: int = 0
    security_blocked_pre_execution: int = 0
    security_breaches_baseline: int = 0
    security_breaches_treatment: int = 0
