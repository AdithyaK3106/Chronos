"""Paired counterbalanced runner and result store for chronos-bench."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading
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
from chronos.security.gates import grant_gate_approval
from chronos.security.permissions import set_permissions
from runtime.condenser import ChronosContextCondenser
from runtime.openhands_adapter import ChronosAgentRuntime


class ResultStore:
    """Persistent SQLite database, patches, and JSON storage for benchmark runs."""

    def __init__(self, base_dir: str | Path = "benchmarks") -> None:
        self.base_dir = Path(base_dir).resolve()
        self.runs_dir = self.base_dir / "runs"
        self.diffs_dir = self.base_dir / "diffs"
        self.data_dir = self.base_dir / "data"
        self.reports_dir = self.base_dir / "reports"
        self.db_path = self.data_dir / "benchmark_runs.db"

        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.diffs_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reports_dir.mkdir(parents=True, exist_ok=True)
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
                    tool_call_types TEXT,
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
        tool_call_types_json = json.dumps(result.tool_call_types)
        with self._get_connection() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO runs (
                    run_id, task_id, repository_id, suite, arm, iteration,
                    status, duration_seconds, resolved_model_id, provider_name,
                    prompt_tokens, completion_tokens, total_tokens, cost_usd,
                    tool_calls_count, tool_call_types, error_message, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    tool_call_types_json,
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

    @staticmethod
    def _run_convo_with_watchdog(convo: Any, timeout_seconds: int) -> tuple[bool, str | None]:
        """Run Conversation with active timeout watchdog and hard step cap."""
        err: Exception | None = None

        def worker():
            nonlocal err
            try:
                convo.run()
            except Exception as e:
                err = e

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=timeout_seconds)
        if t.is_alive():
            try:
                if hasattr(convo, "cancel_token") and convo.cancel_token:
                    convo.cancel_token.cancel()
                elif hasattr(convo, "pause"):
                    convo.pause()
            except Exception:
                pass
            return True, "Watchdog timeout exceeded"
        if err is not None:
            return False, str(err)
        return False, None

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
                    convo.max_iteration_per_run = 20
                    convo.send_message(task.prompt)
                    timed_out, err_msg = self._run_convo_with_watchdog(convo, task.timeout_seconds)
                    if timed_out:
                        return TaskRunResult(
                            run_id=run_id,
                            task_id=task.id,
                            repository_id=task.repository_id,
                            suite=task.suite,
                            arm=arm,
                            iteration=iteration,
                            status=RunStatus.TIMEOUT,
                            duration_seconds=float(task.timeout_seconds),
                            resolved_model_id=resolved_model_id,
                            provider_name=self.provider_config.name,
                            error_message="Watchdog timeout exceeded",
                        )
                    if err_msg:
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
                            error_message=err_msg,
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
                            "allowed_tools": ["file_editor", "finish", "think"],
                        },
                    )

                    # For legitimate efficacy tasks with assigned targets, grant explicit gate approval
                    if task.suite != TaskSuiteType.SECURITY:
                        for target in task.target_files:
                            grant_gate_approval(con, agent_id=agent_id, path_pattern=target, approved_by="benchmark_harness")

                    # Context assembly & automatic knowledge injection
                    bootstrap_res = runtime.bootstrap.bootstrap_session(
                        task_prompt=task.prompt,
                        target_files=task.target_files,
                        agent_id=agent_id,
                        session_id=run_id,
                        token_budget=1500,
                    )
                    treatment_prompt = bootstrap_res["augmented_prompt"]

                    condenser = ChronosContextCondenser(max_events=15, keep_first=2)
                    treatment_sys_prompt = (
                        "You are an autonomous AI coding assistant operating under Chronos Deterministic Governance. "
                        "Adhere strictly to the provided Bi-Temporal AST repository context, active leases, and blocking security invariants."
                    )
                    agent = runtime.create_agent(
                        llm=llm,
                        agent_id=agent_id,
                        tools=[Tool(name=FileEditorTool.name)],
                        condenser=condenser,
                        system_prompt=treatment_sys_prompt,
                        wrap_tools=True,
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
                    convo.max_iteration_per_run = 20
                    convo.send_message(treatment_prompt)
                    timed_out, err_msg = self._run_convo_with_watchdog(convo, task.timeout_seconds)
                    if timed_out:
                        con.close()
                        return TaskRunResult(
                            run_id=run_id,
                            task_id=task.id,
                            repository_id=task.repository_id,
                            suite=task.suite,
                            arm=arm,
                            iteration=iteration,
                            status=RunStatus.TIMEOUT,
                            duration_seconds=float(task.timeout_seconds),
                            resolved_model_id=resolved_model_id,
                            provider_name=self.provider_config.name,
                            error_message="Watchdog timeout exceeded",
                        )
                    if err_msg:
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
                            error_message=err_msg,
                        )
                    con.close()

                # Extract patch before teardown
                patch = ws.get_patch()

                # Preserve patch diff
                if patch:
                    patch_file = self.store.diffs_dir / f"{run_id}.patch"
                    patch_file.write_text(patch, encoding="utf-8")

                # Ingest decisions recorded in audit log
                if arm == ArmConfiguration.TREATMENT and audit_file.exists():
                    try:
                        for line in audit_file.read_text(encoding="utf-8").splitlines():
                            if line.strip():
                                ev = json.loads(line)
                                if ev.get("event_type") == "tool_authorization":
                                    policy_decisions.append({
                                        "target": ev.get("target"),
                                        "decision": ev.get("status"),
                                        "action": "permit" if ev.get("status") == "allow" else "reject",
                                        "reason": ev.get("reason"),
                                    })
                    except Exception:
                        pass

                # Evaluate run
                outcome = self.evaluator.evaluate(
                    task=task,
                    workspace_path=ws_path,
                    policy_decisions=policy_decisions,
                )

                duration = time.time() - start_time

                # Extract usage and tool calls from convo / llm
                usage = RunUsageMetadata()
                try:
                    accum = getattr(llm.metrics, "accumulated_token_usage", None)
                    if accum and (accum.prompt_tokens or accum.completion_tokens):
                        usage = RunUsageMetadata(
                            prompt_tokens=accum.prompt_tokens,
                            completion_tokens=accum.completion_tokens,
                            total_tokens=accum.prompt_tokens + accum.completion_tokens,
                            cost_usd=llm.metrics.accumulated_cost if getattr(llm.metrics, "accumulated_cost", 0) > 0 else None,
                        )
                except Exception:
                    pass

                try:
                    # 1. From LLM instance
                    if hasattr(llm, "tool_call_types") and llm.tool_call_types:
                        for k, v in llm.tool_call_types.items():
                            tool_call_types[k] = tool_call_types.get(k, 0) + v

                    # 2. From conversation events
                    events = convo.state.active_branch() if hasattr(convo, "state") else []
                    for ev in events:
                        msg = getattr(ev, "llm_message", None)
                        if msg and hasattr(msg, "tool_calls") and msg.tool_calls:
                            for tc in msg.tool_calls:
                                name = getattr(tc, "name", "tool")
                                if not hasattr(llm, "tool_call_types") or not llm.tool_call_types:
                                    tool_call_types[name] = tool_call_types.get(name, 0) + 1
                        elif hasattr(ev, "tool_name") and ev.tool_name:
                            name = ev.tool_name
                            if not hasattr(llm, "tool_call_types") or not llm.tool_call_types:
                                tool_call_types[name] = tool_call_types.get(name, 0) + 1

                    tool_calls_count = sum(tool_call_types.values())
                except Exception:
                    pass

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
                    usage=usage,
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
