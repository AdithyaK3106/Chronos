"""Integration tests for Epistemic Living Memory in the Chronos Benchmark Harness."""

import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock

import pytest

from benchmarks.harness.models import (
    ArmConfiguration,
    BenchmarkTask,
    ModelProviderConfig,
    RepositoryConfig,
    RunStatus,
    TaskCategory,
    TaskSuiteType,
)
from benchmarks.harness.runner import PairedRunner
from chronos.core.db import get_connection
from chronos.memory.models import MemoryTier
from chronos.memory.store import ChronosMemoryStore
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.testing import TestLLM


@pytest.fixture
def temp_bench_dir(tmp_path: Path) -> Path:
    bench_dir = tmp_path / "benchmarks"
    bench_dir.mkdir(parents=True, exist_ok=True)
    return bench_dir


@pytest.fixture
def mock_code_repo(tmp_path: Path) -> RepositoryConfig:
    repo_dir = tmp_path / "mock_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    src_file = repo_dir / "calculator.py"
    src_file.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    
    # Init git repo
    import subprocess
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_dir), capture_output=True, check=True)

    return RepositoryConfig(
        id="mock_calc",
        name="Mock Calculator",
        path=str(repo_dir),
        baseline_commit="HEAD",
    )


def test_epistemic_memory_cross_task_transfer_in_benchmark(
    temp_bench_dir: Path, mock_code_repo: RepositoryConfig
):
    """Test that Task 1 distills and stores memory, and Task 2 on the same repo recalls it."""
    mock_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="Task 1 done.")]),
        Message(role="assistant", content=[TextContent(text="Task 2 done.")]),
    ])

    provider_cfg = ModelProviderConfig(name="mock", model="test-mock")
    runner = PairedRunner(
        provider_config=provider_cfg,
        results_dir=temp_bench_dir,
        mock_llm=mock_llm,
    )

    task1 = BenchmarkTask(
        id="calc_task_01",
        repository_id=mock_code_repo.id,
        suite=TaskSuiteType.EFFICACY,
        category=TaskCategory.BUG_FIX,
        prompt="Fix addition bug in calculator.py",
        target_files=["calculator.py"],
        verification_commands=["python -c 'from calculator import add; assert add(2, 2) == 4'"],
    )

    # 1. Run Task 1 (Treatment arm)
    res1 = runner.execute_arm(
        task=task1,
        repo=mock_code_repo,
        arm=ArmConfiguration.TREATMENT,
        iteration=1,
    )

    assert res1.status == RunStatus.SUCCESS
    assert res1.memories_recalled == 0
    assert res1.memories_stored >= 1  # Distilled from successful task

    # 2. Run Task 2 on the same repository
    task2 = BenchmarkTask(
        id="calc_task_02",
        repository_id=mock_code_repo.id,
        suite=TaskSuiteType.EFFICACY,
        category=TaskCategory.FEATURE,
        prompt="Add subtract function to calculator.py",
        target_files=["calculator.py"],
        verification_commands=["python -c 'from calculator import add; assert add(2, 2) == 4'"],
    )

    res2 = runner.execute_arm(
        task=task2,
        repo=mock_code_repo,
        arm=ArmConfiguration.TREATMENT,
        iteration=1,
    )

    assert res2.status == RunStatus.SUCCESS
    # Task 2 MUST recall the memory recorded by Task 1!
    assert res2.memories_recalled >= 1
    assert res2.memories_stored >= 1


def test_epistemic_memory_cross_repo_isolation(
    temp_bench_dir: Path, mock_code_repo: RepositoryConfig, tmp_path: Path
):
    """Test that memories recorded in Repo A are never leaked or recalled in Repo B."""
    mock_llm = TestLLM.from_messages([
        Message(role="assistant", content=[TextContent(text="Task completed.")]),
    ])

    provider_cfg = ModelProviderConfig(name="mock", model="test-mock")
    runner = PairedRunner(
        provider_config=provider_cfg,
        results_dir=temp_bench_dir,
        mock_llm=mock_llm,
    )

    # Setup Repo B
    repo_b_dir = tmp_path / "mock_repo_b"
    repo_b_dir.mkdir(parents=True, exist_ok=True)
    (repo_b_dir / "service.py").write_text("def run(): pass\n", encoding="utf-8")
    import subprocess
    subprocess.run(["git", "init"], cwd=str(repo_b_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_b_dir), capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=str(repo_b_dir), capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo_b_dir), capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(repo_b_dir), capture_output=True, check=True)

    repo_b = RepositoryConfig(id="mock_service_b", name="Service B", path=str(repo_b_dir))

    # Pre-seed a secret memory in Repo A
    repo_db = runner.store.data_dir / "repo_chronos" / f"chronos_{mock_code_repo.id}.db"
    con_a = get_connection(repo_db)
    store_a = ChronosMemoryStore(con_a)
    store_a.remember(
        content="Secret financial formula in calculator.py: apply tax rate 0.15",
        repo_namespace=mock_code_repo.id,
        tier=MemoryTier.LEARNED,
        importance=0.9,
    )
    con_a.close()

    # Run Task on Repo B
    task_b = BenchmarkTask(
        id="service_task_01",
        repository_id=repo_b.id,
        suite=TaskSuiteType.EFFICACY,
        category=TaskCategory.FEATURE,
        prompt="Apply financial formula and tax rate in service.py",
        target_files=["service.py"],
        verification_commands=["python -c 'import service; service.run()'"],
    )

    res_b = runner.execute_arm(
        task=task_b,
        repo=repo_b,
        arm=ArmConfiguration.TREATMENT,
        iteration=1,
    )

    # Repo B MUST have 0 memories recalled from Repo A
    assert res_b.memories_recalled == 0
