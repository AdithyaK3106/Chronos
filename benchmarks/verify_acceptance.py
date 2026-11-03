"""Check that each task's hidden acceptance tests are fair.

For every efficacy task with acceptance tests, in a fresh worktree at the repository's baseline:
  1. untouched code must FAIL the acceptance tests (otherwise they do not measure the task);
  2. the reference fix (acceptance/<repo>/reference/<task_id>.patch) must make the task pass
     the full evaluation: verification commands, regression commands and acceptance tests.

It uses the harness's own ObjectiveEvaluator and repository test_venv, so it checks exactly what a
benchmark run checks. Reference patches live in this repo, never in the agent's workspace.

    python -m benchmarks.verify_acceptance [--task ID ...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from benchmarks.harness.evaluator import ObjectiveEvaluator
from benchmarks.harness.models import RunStatus, TaskSuiteType
from benchmarks.harness.registry import TaskRegistry
from benchmarks.harness.workspace import WorktreeManager

SUITE = Path(__file__).parent / "suites" / "enterprise_24"


def reference_patch(task) -> Path:
    return SUITE / "acceptance" / task.repository_id / "reference" / f"{task.id}.patch"


def check(task, repo) -> tuple[bool, str]:
    evaluator = ObjectiveEvaluator()
    with WorktreeManager(repo.path, commit=task.starting_commit or repo.baseline_commit) as mgr:
        base = evaluator.evaluate(task, mgr.workspace_path, test_venv=repo.test_venv)
        acc_failed = base.status != RunStatus.SUCCESS and "Acceptance tests failed" in (base.error_message or "")
        if not acc_failed:
            return False, f"baseline: expected acceptance failure, got {base.status.value}: {(base.error_message or '')[:160]}"

    patch = reference_patch(task)
    if not patch.is_file():
        return False, f"no reference patch at {patch.relative_to(SUITE)}"
    with WorktreeManager(repo.path, commit=task.starting_commit or repo.baseline_commit) as mgr:
        ws = mgr.workspace_path
        applied = subprocess.run(["git", "-C", str(ws), "apply", str(patch.resolve())], capture_output=True, text=True)
        if applied.returncode != 0:
            return False, f"reference patch does not apply: {applied.stderr.strip()[:200]}"
        fixed = evaluator.evaluate(task, ws, test_venv=repo.test_venv)
        if fixed.status != RunStatus.SUCCESS:
            tail = (fixed.stdout or "")[-300:] + (fixed.stderr or "")[-300:]
            return False, f"reference fix: {fixed.status.value}: {(fixed.error_message or '')[:160]} | {tail.strip()[-300:]}"
    return True, "fails on baseline, passes with reference fix"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", action="append", help="Only these task ids (repeatable)")
    args = ap.parse_args()

    reg = TaskRegistry()
    reg.load_suite(SUITE)
    tasks = [
        t for t in reg.tasks.values()
        if t.suite == TaskSuiteType.EFFICACY and t.acceptance_tests and (not args.task or t.id in args.task)
    ]
    if not tasks:
        print("no matching tasks with acceptance tests")
        return 1
    failures = 0
    for task in sorted(tasks, key=lambda t: t.id):
        ok, msg = check(task, reg.repositories[task.repository_id])
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {task.id:22} {msg}", flush=True)
    print(f"\n{len(tasks) - failures}/{len(tasks)} tasks have fair acceptance tests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
