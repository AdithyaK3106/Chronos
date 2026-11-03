"""Hidden acceptance tests in the benchmark evaluator."""

from __future__ import annotations

import sys
from pathlib import Path

from benchmarks.harness.evaluator import ObjectiveEvaluator
from benchmarks.harness.models import BenchmarkTask, RunStatus, TaskSuiteType


def _task(tmp_path: Path, **extra) -> BenchmarkTask:
    hidden = tmp_path / "hidden" / "test_hidden_add.py"
    hidden.parent.mkdir()
    hidden.write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    return BenchmarkTask(
        id="t1",
        repository_id="r",
        suite=TaskSuiteType.EFFICACY,
        prompt="make add() add",
        verification_commands=[f'"{sys.executable}" -c "import calc"'],
        acceptance_tests=[str(hidden)],
        timeout_seconds=60,
        **extra,
    )


def _workspace(tmp_path: Path, body: str) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calc.py").write_text(body)
    return ws


def test_passing_existing_checks_is_not_enough(tmp_path):
    ws = _workspace(tmp_path, "def add(a, b):\n    return a - b\n")
    out = ObjectiveEvaluator().evaluate(_task(tmp_path), ws, test_venv=str(Path(sys.executable).parent.parent))
    assert out.status == RunStatus.TASK_FAILURE
    assert "Acceptance tests failed" in (out.error_message or "")


def test_correct_change_passes_and_hidden_tests_are_removed(tmp_path):
    ws = _workspace(tmp_path, "def add(a, b):\n    return a + b\n")
    out = ObjectiveEvaluator().evaluate(_task(tmp_path), ws, test_venv=str(Path(sys.executable).parent.parent))
    assert out.status == RunStatus.SUCCESS
    assert not (ws / ObjectiveEvaluator.ACCEPTANCE_DIR).exists()


def test_task_without_acceptance_tests_says_completion_was_not_checked(tmp_path):
    ws = _workspace(tmp_path, "def add(a, b):\n    return a - b\n")
    task = _task(tmp_path).model_copy(update={"acceptance_tests": []})
    out = ObjectiveEvaluator().evaluate(task, ws)
    assert out.status == RunStatus.SUCCESS
    assert "not checked" in out.stdout


def test_suite_paths_resolve_relative_to_suite(tmp_path):
    from benchmarks.harness.registry import TaskRegistry

    reg = TaskRegistry()
    reg.load_suite(Path(__file__).parent.parent / "benchmarks" / "suites" / "enterprise_24")
    task = reg.filter_tasks(task_ids=["cng_task_02"])[0]
    assert task.acceptance_tests and all(Path(p).is_file() for p in task.acceptance_tests)
