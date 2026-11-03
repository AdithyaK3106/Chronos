"""The orchestration loop behind `chronos run --engine claude`.

    resolve context -> engine works (in the sandbox) -> run the task's tests
        -> pass: done          -> fail: resume the same session with the failures (max N rounds)
    -> record the outcome as a memory and as a run in the dashboard's run registry

The tests decide success, not the agent's own report.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from chronos.orchestrator.engines import CommandRunner, Engine, EngineResult, default_runner

FEEDBACK_TAIL_CHARS = 3000


@dataclass
class RoundRecord:
    round: int
    engine_ok: bool
    tests_passed: bool
    test_exit: int
    engine_error: Optional[str] = None
    cost_usd: Optional[float] = None


@dataclass
class RunOutcome:
    run_id: str
    success: bool
    rounds: list[RoundRecord] = field(default_factory=list)
    session_id: Optional[str] = None
    cost_usd: float = 0.0
    context_chars: int = 0
    sandboxed: bool = False
    final_test_output: str = ""
    error: Optional[str] = None


def sandboxed_runner(root: Path, agent_id: str) -> CommandRunner:
    """A CommandRunner that runs every command inside the `chronos exec` sandbox for `root`.

    Raises SandboxUnavailable when the sandbox cannot be created; it never silently falls back to
    running unprotected.
    """
    from chronos.core.db import get_connection
    from chronos.sandbox.plan import plan_sandbox
    from chronos.sandbox.runner import _require_bwrap, build_command

    bwrap = _require_bwrap()
    con = get_connection(root / ".chronos" / "chronos.db")
    try:
        plan = plan_sandbox(root, agent_id=agent_id, con=con)
    finally:
        con.close()

    def run(argv: list[str], cwd: Path, timeout_s: int) -> tuple[int, str, str]:
        return default_runner(build_command(plan, argv, bwrap, chdir=str(cwd)), cwd, timeout_s)

    return run


def resolve_context(root: Path, prompt: str, agent_id: str) -> str:
    """Chronos context for the task (same source as the Claude Code prompt hook), or '' if none."""
    from chronos.context.session import ChronosSessionBootstrap
    from chronos.core.config import ChronosConfig
    from chronos.core.db import get_connection
    from chronos.integrations.claude_code import ensure_index_fresh, frame_untrusted_context

    con = get_connection(root / ".chronos" / "chronos.db")
    try:
        ensure_index_fresh(root, con)
        res = ChronosSessionBootstrap(repo_path=root, con=con).bootstrap_session(
            task_prompt=prompt, agent_id=agent_id, session_id="chronos-run", refresh_index=False, acquire_leases=False,
        )
    finally:
        con.close()
    if not res.get("candidates"):
        return ""
    try:
        max_chars = ChronosConfig.load(root).policy.max_context_chars
    except Exception:  # noqa: BLE001
        max_chars = 6000
    return frame_untrusted_context(res["envelope_text"], max_chars)


def first_prompt(task: str, context: str, test_command: str) -> str:
    parts = [task.strip()]
    if context:
        parts.append(context)
    parts.append(f"When you are done, the change is checked by running: {test_command}")
    return "\n\n".join(parts)


def feedback_prompt(test_command: str, output: str, round_no: int, max_rounds: int) -> str:
    tail = output[-FEEDBACK_TAIL_CHARS:]
    return (
        f"The tests still fail (round {round_no} of {max_rounds}). `{test_command}` printed:\n\n"
        f"```\n{tail}\n```\n\nFix the code so these tests pass. Do not weaken or delete the tests."
    )


def run_tests(test_command: str, root: Path, run: CommandRunner, timeout_s: int) -> tuple[int, str]:
    # No bytecode: a quick edit that keeps a file's size within the same second would otherwise
    # let the next round run the previous round's stale .pyc (mtime has 1 s resolution).
    if os.name == "nt":
        argv = ["cmd", "/c", f"set PYTHONDONTWRITEBYTECODE=1&& {test_command}"]
    else:
        argv = ["env", "PYTHONDONTWRITEBYTECODE=1", "sh", "-c", test_command]
    code, out, err = run(argv, root, timeout_s)
    return code, (out + ("\n" + err if err else "")).strip()


def run_task(
    repo: str | Path,
    task: str,
    engine: Engine,
    test_command: str,
    max_rounds: int = 3,
    sandbox: bool = True,
    agent_id: str = "claude_code",
    test_timeout_s: int = 600,
    runner: Optional[CommandRunner] = None,
    use_context: bool = True,
) -> RunOutcome:
    """Run `task` with `engine` until `test_command` passes or `max_rounds` is reached."""
    from chronos.core.db import get_connection
    from chronos.core.repo import inspect_repository
    from chronos.telemetry.runs import create_run, log_run_event, update_run

    root = Path(repo).resolve()
    (root / ".chronos").mkdir(parents=True, exist_ok=True)
    identity = inspect_repository(root)
    if runner is None:
        runner = sandboxed_runner(root, agent_id) if sandbox else default_runner
    context = resolve_context(root, task, agent_id) if use_context else ""

    con = get_connection(root / ".chronos" / "chronos.db")
    started = time.monotonic()
    run = create_run(
        con, repo_id=identity.id, task_description=task, agent_id=agent_id, provider_name=engine.name,
        model_id=getattr(engine, "model", None), arm="chronos_run", commit_sha=identity.commit,
        metadata={"test_command": test_command, "max_rounds": max_rounds, "sandbox": sandbox, "context_chars": len(context)},
    )
    outcome = RunOutcome(run_id=run["run_id"], success=False, context_chars=len(context), sandboxed=sandbox)
    try:
        prompt = first_prompt(task, context, test_command)
        for round_no in range(1, max_rounds + 1):
            if round_no == 1:
                res: EngineResult = engine.start(prompt, root, runner)
            else:
                res = engine.resume(outcome.session_id, prompt, root, runner)
            outcome.session_id = res.session_id or outcome.session_id
            outcome.cost_usd += res.cost_usd or 0.0
            code, output = run_tests(test_command, root, runner, test_timeout_s)
            rec = RoundRecord(round_no, res.ok, code == 0, code, res.error, res.cost_usd)
            outcome.rounds.append(rec)
            outcome.final_test_output = output
            log_run_event(con, outcome.run_id, "round_completed", {
                "round": round_no, "engine_ok": res.ok, "engine_error": res.error,
                "tests_passed": code == 0, "test_exit": code, "cost_usd": res.cost_usd,
            })
            if code == 0:
                outcome.success = True
                break
            if not res.ok and not res.session_id:
                outcome.error = f"engine failed: {res.error}"
                break
            prompt = feedback_prompt(test_command, output, round_no + 1, max_rounds)
    except Exception as exc:  # noqa: BLE001 - record the failure, then re-raise
        outcome.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _record_memory(con, identity.name, agent_id, task, test_command, outcome)
        update_run(
            con, outcome.run_id, status="SUCCESS" if outcome.success else "FAILED",
            duration_seconds=round(time.monotonic() - started, 2), cost_usd=outcome.cost_usd,
            error_message=outcome.error or (None if outcome.success else "tests still failing"),
            metadata={"rounds": len(outcome.rounds), "session_id": outcome.session_id},
        )
        log_run_event(con, outcome.run_id, "run_finished", {"success": outcome.success, "rounds": len(outcome.rounds)})
        con.close()
    return outcome


def _record_memory(con, namespace: str, agent_id: str, task: str, test_command: str, outcome: RunOutcome) -> None:
    """Remember how the task went, so the next session can recall it (tests decided, not the agent)."""
    from chronos.memory.models import MemoryTier, OutcomeType, VerificationStatus
    from chronos.memory.store import ChronosMemoryStore

    n = len(outcome.rounds)
    if outcome.success:
        content = f"Task solved by {agent_id} in {n} round(s); `{test_command}` passes. Task: {task}"
    else:
        last = outcome.final_test_output[-600:]
        content = f"Task NOT solved by {agent_id} after {n} round(s); `{test_command}` still fails. Task: {task}\nLast output:\n{last}"
    try:
        ChronosMemoryStore(con=con).remember(
            content=content,
            summary=("Solved: " if outcome.success else "Unsolved: ") + task[:120],
            repo_namespace=namespace,
            tier=MemoryTier.EPISODIC,
            source="chronos_run",
            task_id=outcome.run_id,
            agent_id=agent_id,
            outcome=OutcomeType.SUCCESS if outcome.success else OutcomeType.FAILURE,
            verification_status=VerificationStatus.VERIFIED if outcome.success else VerificationStatus.UNVERIFIED,
        )
    except Exception:  # noqa: BLE001 - memory is a convenience; never hide the run's result
        pass


__all__ = ["RunOutcome", "RoundRecord", "run_task", "resolve_context", "sandboxed_runner"]
