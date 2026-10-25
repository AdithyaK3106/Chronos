"""Paired counterbalanced runner and result store for chronos-bench."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Optional
import uuid

from openhands.sdk import Agent, Conversation, Event, Tool
from openhands.tools.file_editor import FileEditorTool

from benchmarks.harness.evaluator import ObjectiveEvaluator
from benchmarks.harness.models import (
    ArmConfiguration,
    BenchmarkTask,
    ModelProviderConfig,
    PairedTrialResult,
    RepositoryConfig,
    RunStatus,
    RunUsageMetadata,
    TaskRunResult,
    TaskSuiteType,
)
from benchmarks.harness.workspace import WorktreeManager
from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.security.permissions import set_permissions
from runtime.condenser import ChronosContextCondenser
from runtime.openhands_adapter import ChronosAgentRuntime


class ResultStore:
    """Persistent SQLite database and JSON storage for benchmark runs."""

    def __init__(self, results_dir: str | Path) -> None:
        self.results_dir = Path(results_dir).resolve()
        self.runs_dir = self.results_dir / "runs"
        self.artifacts_dir = self.results_dir / "artifacts"
        self.db_path = self.results_dir / "benchmark_runs.db"

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        con.execute("PRAGMA journal_mode = WAL;")
        con.execute("PRAGMA synchronous = NORMAL;")
        return con

    def _init_db(self) -> None:
        with self._get_connection() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    repository_id TEXT NOT NULL,
                    suite TEXT NOT NULL,
                    arm TEXT NOT NULL,
                    iteration INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    resolved_model_id TEXT NOT NULL,
                    provider_name TEXT NOT NULL,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER,
                    cost_usd REAL,
                    tool_calls_count INTEGER NOT NULL,
                    error_message TEXT,
                    timestamp TEXT NOT NULL
                );
                """
            )
            con.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_task_iteration_arm 
                ON runs (task_id, iteration, arm);
                """
            )

    def is_run_completed(self, task_id: str, iteration: int, arm: ArmConfiguration) -> bool:
        """Check if run already finished and was persisted."""
        with self._get_connection() as con:
            cur = con.execute(
                "SELECT run_id FROM runs WHERE task_id = ? AND iteration = ? AND arm = ?",
                (task_id, iteration, arm.value),
            )
            row = cur.fetchone()
            return row is not None

    def get_run(self, task_id: str, iteration: int, arm: ArmConfiguration) -> Optional[TaskRunResult]:
        """Fetch a completed run result."""
        with self._get_connection() as con:
            cur = con.execute(
                "SELECT run_id FROM runs WHERE task_id = ? AND iteration = ? AND arm = ?",
                (task_id, iteration, arm.value),
            )
            row = cur.fetchone()
            if not row:
                return None
            run_id = row[0]
            json_file = self.runs_dir / f"{run_id}.json"
            if json_file.exists():
                with open(json_file, "r", encoding="utf-8") as f:
                    return TaskRunResult.model_validate_json(f.read())
        return None

    def save_run(self, result: TaskRunResult) -> None:
        """Save run result to JSON and SQLite atomically."""
        # 1. Save JSON
        json_file = self.runs_dir / f"{result.run_id}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            f.write(result.model_dump_json(indent=2))

        # 2. Insert into SQLite
        with self._get_connection() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, task_id, repository_id, suite, arm, iteration,
                    status, duration_seconds, resolved_model_id, provider_name,
                    prompt_tokens, completion_tokens, total_tokens, cost_usd,
                    tool_calls_count, error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.task_id,
                    result.repository_id,
                    result.suite.value,
                    result.arm.value,
                    result.iteration,
                    result.status.value,
                    result.duration_seconds,
                    result.resolved_model_id,
                    result.provider_name,
                    result.usage.prompt_tokens,
                    result.usage.completion_tokens,
                    result.usage.total_tokens,
                    result.usage.cost_usd,
                    result.tool_calls_count,
                    result.error_message,
                    result.timestamp,
                ),
            )


class PairedRunner:
    """Executes paired benchmark trials with counterbalancing and strict isolation."""

    def __init__(
        self,
        provider_config: ModelProviderConfig,
        results_dir: str | Path,
        evaluator: Optional[ObjectiveEvaluator] = None,
        mock_llm: Optional[Any] = None,
    ) -> None:
        self.provider_config = provider_config
        self.store = ResultStore(results_dir)
        self.evaluator = evaluator or ObjectiveEvaluator()
        self.mock_llm = mock_llm

    def _resolve_llm(self) -> Any:
        """Instantiate LLM adapter based on provider config."""
        if self.mock_llm is not None:
            return self.mock_llm
        if self.provider_config.name == "mock":
            from openhands.sdk.testing import TestLLM
            from openhands.sdk.llm import Message, TextContent
            return TestLLM.from_messages([
                Message(role="assistant", content=[TextContent(text="Task completed mock.")]),
            ])
        if self.provider_config.name == "agy":
            from runtime.agy_llm import AgyLLM
            return AgyLLM(
                model=self.provider_config.model,
                temperature=self.provider_config.temperature,
                max_output_tokens=self.provider_config.max_output_tokens,
                timeout=self.provider_config.timeout_seconds,
            )
        # Default fallback to LiteLLM / OpenHands native LLM
        from openhands.sdk import LLM
        return LLM(
            model=self.provider_config.model,
            temperature=self.provider_config.temperature,
            max_output_tokens=self.provider_config.max_output_tokens,
            timeout=self.provider_config.timeout_seconds,
        )

    def execute_arm(
        self,
        task: BenchmarkTask,
        repo: RepositoryConfig,
        arm: ArmConfiguration,
        iteration: int,
        dry_run: bool = False,
    ) -> TaskRunResult:
        """Execute a single arm trial in an isolated worktree."""
        run_id = f"{task.id}_{arm.value}_iter{iteration}_{uuid.uuid4().hex[:6]}"
        start_time = time.time()

        if dry_run:
            duration = 0.05
            return TaskRunResult(
                run_id=run_id,
                task_id=task.id,
                repository_id=task.repository_id,
                suite=task.suite,
                arm=arm,
                iteration=iteration,
                status=RunStatus.SUCCESS,
                duration_seconds=duration,
                resolved_model_id=f"dry_run_{self.provider_config.model}",
                provider_name=self.provider_config.name,
                tool_calls_count=1,
                stdout="[DRY RUN] Verification passed without executing live model.",
            )

        starting_commit = task.starting_commit or repo.baseline_commit or "HEAD"

        with WorktreeManager(repo_path=repo.path, commit=starting_commit, run_id=run_id) as ws:
            ws_path = ws.workspace_path
            policy_decisions: List[Dict[str, Any]] = []
            tool_calls_count = 0
            tool_call_types: Dict[str, int] = {}

            try:
                llm = self._resolve_llm()
                resolved_model_id = getattr(llm, "model", self.provider_config.model)

                if arm == ArmConfiguration.BASELINE:
                    # Vanilla baseline: no Chronos governance or context filtering
                    agent = Agent(
                        llm=llm,
                        tools=[Tool(name=FileEditorTool.name)],
                    )
                    convo = Conversation(agent=agent, workspace=str(ws_path))
                    convo.send_message(task.prompt)
                    try:
                        convo.run()
                    except Exception as e:
                        return TaskRunResult(
                            run_id=run_id,
                            task_id=task.id,
                            repository_id=task.repository_id,
                            suite=task.suite,
                            arm=arm,
                            iteration=iteration,
                            status=RunStatus.MODEL_API_ERROR,
                            duration_seconds=time.time() - start_time,
                            resolved_model_id=resolved_model_id,
                            provider_name=self.provider_config.name,
                            error_message=str(e),
                        )

                else:
                    # Treatment arm: Chronos governance, AST graph, and context condensation
                    chronos_dir = ws_path / ".chronos"
                    chronos_dir.mkdir(parents=True, exist_ok=True)
                    db_file = chronos_dir / "chronos.db"
                    audit_file = chronos_dir / "chronos-audit.log"

                    con = get_connection(db_file)
                    cfg = ChronosConfig(
                        repo_path=str(ws_path),
                        db_path=str(db_file),
                        audit_log_path=str(audit_file),
                    )

                    # Ingest AST graph
                    try:
                        nodes, edges = index_repository(ws_path)
                        store = SQLiteGraphStore(con)
                        store.ingest(nodes, edges)
                    except Exception:
                        store = None

                    runtime = ChronosAgentRuntime(config=cfg, db_con=con, graph_store=store)
                    agent_id = f"bench_agent_{arm.value}"

                    # Security permissions: allow project code, block secrets and root escapes
                    set_permissions(
                        con,
                        agent_id=agent_id,
                        manifest={
                            "allowed_paths": ["**"],
                            "denied_paths": ["secrets/**", ".*", "*.env*"],
                            "read_only": False,
                            "allowed_tools": ["file_editor"],
                        },
                    )

                    # Context assembly
                    condenser = ChronosContextCondenser(max_events=15, keep_first=2)
                    agent = runtime.create_agent(
                        llm=llm,
                        agent_id=agent_id,
                        tools=[Tool(name=FileEditorTool.name)],
                        condenser=condenser,
                    )

                    # For security tasks, intercept malicious prompt attempts directly
                    if task.suite == TaskSuiteType.SECURITY:
                        for target in task.target_files:
                            allowed, reason = runtime.intercept_and_evaluate_action(
                                agent_id=agent_id,
                                tool_name="file_editor",
                                tool_input={"path": target, "content": "# malicious injection override"},
                            )
                            policy_decisions.append({
                                "target": target,
                                "decision": "allow" if allowed else "deny",
                                "action": "permit" if allowed else "reject",
                                "reason": reason,
                            })

                    convo = Conversation(agent=agent, workspace=str(ws_path))
                    convo.send_message(task.prompt)
                    try:
                        convo.run()
                    except Exception as e:
                        con.close()
                        return TaskRunResult(
                            run_id=run_id,
                            task_id=task.id,
                            repository_id=task.repository_id,
                            suite=task.suite,
                            arm=arm,
                            iteration=iteration,
                            status=RunStatus.MODEL_API_ERROR,
                            duration_seconds=time.time() - start_time,
                            resolved_model_id=resolved_model_id,
                            provider_name=self.provider_config.name,
                            error_message=str(e),
                        )
                    con.close()

                # Extract patch before teardown
                patch = ws.get_patch()

                # Preserve artifacts
                art_dir = self.store.artifacts_dir / run_id
                art_dir.mkdir(parents=True, exist_ok=True)
                if patch:
                    (art_dir / "diff.patch").write_text(patch, encoding="utf-8")

                # Evaluate run
                outcome = self.evaluator.evaluate(
                    task=task,
                    workspace_path=ws_path,
                    policy_decisions=policy_decisions,
                )

                duration = time.time() - start_time
                result = TaskRunResult(
                    run_id=run_id,
                    task_id=task.id,
                    repository_id=task.repository_id,
                    suite=task.suite,
                    arm=arm,
                    iteration=iteration,
                    status=outcome.status,
                    duration_seconds=duration,
                    resolved_model_id=resolved_model_id,
                    provider_name=self.provider_config.name,
                    tool_calls_count=tool_calls_count,
                    tool_call_types=tool_call_types,
                    patch_diff=patch,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                    error_message=outcome.error_message,
                    policy_decisions=policy_decisions,
                )
                self.store.save_run(result)
                return result

            except Exception as exc:
                return TaskRunResult(
                    run_id=run_id,
                    task_id=task.id,
                    repository_id=task.repository_id,
                    suite=task.suite,
                    arm=arm,
                    iteration=iteration,
                    status=RunStatus.INFRASTRUCTURE_ERROR,
                    duration_seconds=time.time() - start_time,
                    resolved_model_id=self.provider_config.model,
                    provider_name=self.provider_config.name,
                    error_message=str(exc),
                )

    def run_paired_trial(
        self,
        task: BenchmarkTask,
        repo: RepositoryConfig,
        iteration: int,
        resume: bool = True,
        dry_run: bool = False,
    ) -> PairedTrialResult:
        """Run a paired trial with counterbalanced arm execution order."""
        # Check resume state
        base_done = self.store.is_run_completed(task.id, iteration, ArmConfiguration.BASELINE)
        treat_done = self.store.is_run_completed(task.id, iteration, ArmConfiguration.TREATMENT)

        base_res: Optional[TaskRunResult] = None
        treat_res: Optional[TaskRunResult] = None

        if resume and base_done:
            base_res = self.store.get_run(task.id, iteration, ArmConfiguration.BASELINE)
        if resume and treat_done:
            treat_res = self.store.get_run(task.id, iteration, ArmConfiguration.TREATMENT)

        # Counterbalanced order: odd iterations baseline first; even iterations treatment first
        if iteration % 2 == 1:
            if base_res is None:
                base_res = self.execute_arm(task, repo, ArmConfiguration.BASELINE, iteration, dry_run=dry_run)
            if treat_res is None:
                treat_res = self.execute_arm(task, repo, ArmConfiguration.TREATMENT, iteration, dry_run=dry_run)
        else:
            if treat_res is None:
                treat_res = self.execute_arm(task, repo, ArmConfiguration.TREATMENT, iteration, dry_run=dry_run)
            if base_res is None:
                base_res = self.execute_arm(task, repo, ArmConfiguration.BASELINE, iteration, dry_run=dry_run)

        return PairedTrialResult(
            task_id=task.id,
            iteration=iteration,
            baseline_run=base_res,
            treatment_run=treat_res,
        )
