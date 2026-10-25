"""Unit and integration test suite for chronos-bench universal harness."""

import os
from pathlib import Path
import subprocess
import tempfile
import pytest

from openhands.sdk.testing import TestLLM
from openhands.sdk.llm import Message, TextContent

from benchmarks.harness.evaluator import ObjectiveEvaluator
from benchmarks.harness.models import (
    ArmConfiguration,
    BenchmarkTask,
    ModelProviderConfig,
    RepositoryConfig,
    RunStatus,
    TaskCategory,
    TaskSuiteType,
)
from benchmarks.harness.registry import TaskRegistry, resolve_path
from benchmarks.harness.runner import PairedRunner, ResultStore
from benchmarks.harness.stats import compute_metrics, mcnemar_exact_p_value, paired_bootstrap_ci
from benchmarks.harness.workspace import WorktreeManager


@pytest.fixture
def mock_git_repo(tmp_path):
    """Create a temporary git repository with initial commit."""
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=str(repo_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)

    test_file = repo_dir / "app.py"
    test_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), check=True, capture_output=True)
    return repo_dir


def test_registry_env_var_and_path_resolution(tmp_path):
    """Verify registry resolves environment variables with defaults, tildes, and relative paths."""
    resolved = resolve_path("${NON_EXISTENT_BENCH_VAR:-/tmp/fallback_path}")
    assert str(resolved) == "/tmp/fallback_path"

    os.environ["CHRONOS_TEST_DIR"] = str(tmp_path)
    resolved_env = resolve_path("${CHRONOS_TEST_DIR}")
    assert str(resolved_env) == str(tmp_path.resolve())


def test_adhoc_task_registry_creation(tmp_path):
    """Verify TaskRegistry can create dynamic ad-hoc tasks without YAML."""
    registry = TaskRegistry.create_adhoc(
        repo_path=str(tmp_path),
        prompt="Fix null pointer exception",
        verify_cmd="python3 -m unittest",
        regression_cmd="python3 -m unittest discover",
        suite=TaskSuiteType.EFFICACY,
    )
    assert len(registry.repositories) == 1
    assert len(registry.tasks) == 1
    task = list(registry.tasks.values())[0]
    assert task.prompt == "Fix null pointer exception"
    assert task.verification_commands == ["python3 -m unittest"]
    assert task.regression_commands == ["python3 -m unittest discover"]


def test_workspace_isolation_and_diff(mock_git_repo):
    """Verify worktree is isolated, captures diffs, and does not mutate source repo."""
    run_id = "test_run_isolation"
    with WorktreeManager(repo_path=mock_git_repo, commit="HEAD", run_id=run_id) as ws:
        ws_path = ws.workspace_path
        assert ws_path.exists()

        # Modify file inside worktree
        modified_file = ws_path / "app.py"
        modified_file.write_text("def add(a, b):\n    return a + b + 1\n", encoding="utf-8")

        # Extract patch
        patch = ws.get_patch()
        assert "+    return a + b + 1" in patch

    # Check that worktree is cleaned up and source repo remains untouched
    assert not ws_path.exists()
    src_content = (mock_git_repo / "app.py").read_text(encoding="utf-8")
    assert "return a + b\n" in src_content


def test_evaluator_command_classification(tmp_path):
    """Verify evaluator maps shell exit codes to appropriate RunStatus."""
    evaluator = ObjectiveEvaluator()
    task_success = BenchmarkTask(
        id="t1",
        repository_id="r1",
        prompt="p",
        verification_commands=["python3 -c 'exit(0)'"],
        regression_commands=["python3 -c 'exit(0)'"],
    )
    res_success = evaluator.evaluate(task_success, tmp_path)
    assert res_success.status == RunStatus.SUCCESS

    task_fail = BenchmarkTask(
        id="t2",
        repository_id="r1",
        prompt="p",
        verification_commands=["python3 -c 'exit(1)'"],
    )
    res_fail = evaluator.evaluate(task_fail, tmp_path)
    assert res_fail.status == RunStatus.TASK_FAILURE

    task_regr = BenchmarkTask(
        id="t3",
        repository_id="r1",
        prompt="p",
        verification_commands=["python3 -c 'exit(0)'"],
        regression_commands=["python3 -c 'exit(2)'"],
    )
    res_regr = evaluator.evaluate(task_regr, tmp_path)
    assert res_regr.status == RunStatus.REGRESSION_FAILURE


def test_evaluator_security_decision(tmp_path):
    """Verify evaluator classifies security interceptions vs breaches."""
    evaluator = ObjectiveEvaluator()
    task_sec = BenchmarkTask(
        id="t_sec",
        repository_id="r1",
        suite=TaskSuiteType.SECURITY,
        category=TaskCategory.SECURITY_CHALLENGE,
        prompt="Bypass auth",
        target_files=["secrets.env"],
    )

    # Intercepted and blocked
    decisions = [{"target": "secrets.env", "decision": "deny", "action": "reject"}]
    res_blocked = evaluator.evaluate(task_sec, tmp_path, policy_decisions=decisions)
    assert res_blocked.status == RunStatus.BLOCKED_PRE_EXECUTION


def test_statistical_engine_mcnemar_and_bootstrap():
    """Verify McNemar exact test and Paired Bootstrap CI calculations."""
    # When discordant pairs are symmetric (n10=5, n01=5), p-value must be 1.0
    p_sym = mcnemar_exact_p_value(5, 5)
    assert p_sym == 1.0

    # When discordant pairs are highly skewed (n10=0, n01=10), p-value must be < 0.01
    p_skew = mcnemar_exact_p_value(0, 10)
    assert p_skew < 0.01

    # Bootstrap CI
    pairs = [(0, 1)] * 8 + [(1, 1)] * 2
    ci_lower, ci_upper = paired_bootstrap_ci(pairs, n_iterations=1000)
    assert ci_lower <= ci_upper
    assert ci_lower > 0.0  # Significant positive treatment effect


def test_result_store_sqlite_persistence(tmp_path):
    """Verify ResultStore saves and retrieves runs with resume support."""
    store = ResultStore(tmp_path / "results")
    assert not store.is_run_completed("task_01", 1, ArmConfiguration.BASELINE)

    run = BenchmarkTask(id="task_01", repository_id="r1", prompt="test")
    from benchmarks.harness.models import TaskRunResult
    result = TaskRunResult(
        run_id="run_123",
        task_id="task_01",
        repository_id="r1",
        suite=TaskSuiteType.EFFICACY,
        arm=ArmConfiguration.BASELINE,
        iteration=1,
        status=RunStatus.SUCCESS,
        duration_seconds=1.5,
        resolved_model_id="mock_model",
        provider_name="mock",
    )
    store.save_run(result)

    assert store.is_run_completed("task_01", 1, ArmConfiguration.BASELINE)
    retrieved = store.get_run("task_01", 1, ArmConfiguration.BASELINE)
    assert retrieved is not None
    assert retrieved.run_id == "run_123"
    assert retrieved.status == RunStatus.SUCCESS


def test_paired_runner_mock_execution(mock_git_repo, tmp_path):
    """Test end-to-end paired execution using TestLLM."""
    mock_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="Applied code modification.")]),
        Message(role="assistant", content=[TextContent(text="Applied code modification.")]),
        Message(role="assistant", content=[TextContent(text="Applied code modification.")]),
        Message(role="assistant", content=[TextContent(text="Applied code modification.")]),
    ])

    cfg = ModelProviderConfig(name="mock", model="test_model")
    runner = PairedRunner(provider_config=cfg, results_dir=tmp_path / "results", mock_llm=mock_llm)

    task = BenchmarkTask(
        id="task_mock_01",
        repository_id="mock_repo",
        prompt="Fix add function",
        verification_commands=["python3 -c 'exit(0)'"],
    )
    repo = RepositoryConfig(id="mock_repo", name="Mock Repo", path=str(mock_git_repo), baseline_commit="HEAD")

    # Run iteration 1
    trial = runner.run_paired_trial(task=task, repo=repo, iteration=1, dry_run=False)
    assert trial.baseline_run is not None
    assert trial.treatment_run is not None
    assert trial.baseline_run.status == RunStatus.SUCCESS
    assert trial.treatment_run.status == RunStatus.SUCCESS

    # Test resume: running again should return existing results without re-executing
    trial_resumed = runner.run_paired_trial(task=task, repo=repo, iteration=1, resume=True)
    assert trial_resumed.baseline_run.run_id == trial.baseline_run.run_id
    assert trial_resumed.treatment_run.run_id == trial.treatment_run.run_id
