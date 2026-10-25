"""Universal Task & Repository Registry for chronos-bench."""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Dict, List, Optional
import yaml

from benchmarks.harness.models import (
    BenchmarkTask,
    RepositoryConfig,
    TaskCategory,
    TaskSuiteType,
)


def expand_env_vars_with_defaults(text: str) -> str:
    """Expand environment variables with bash-style defaults like ${VAR:-default} and $VAR."""
    pattern = re.compile(r'\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}')

    def replace(match: re.Match) -> str:
        var_name = match.group(1)
        default_val = match.group(2) if match.group(2) is not None else ""
        return os.environ.get(var_name, default_val)

    expanded = pattern.sub(replace, text)
    return os.path.expandvars(expanded)


def resolve_path(path_str: str, base_dir: Optional[Path] = None) -> Path:
    """Resolve environment variables (with defaults), user tilde, and relative paths."""
    expanded = expand_env_vars_with_defaults(path_str)
    expanded = os.path.expanduser(expanded)
    p = Path(expanded)
    if not p.is_absolute() and base_dir is not None:
        p = (base_dir / p).resolve()
    else:
        p = p.resolve()
    return p


class TaskRegistry:
    """Agnostic loader and validator for repositories and benchmark tasks."""

    def __init__(self) -> None:
        self.repositories: Dict[str, RepositoryConfig] = {}
        self.tasks: Dict[str, BenchmarkTask] = {}

    def register_repository(self, repo: RepositoryConfig) -> None:
        """Register a repository config."""
        self.repositories[repo.id] = repo

    def register_task(self, task: BenchmarkTask) -> None:
        """Register a benchmark task."""
        if task.repository_id not in self.repositories:
            raise ValueError(
                f"Task '{task.id}' references unknown repository '{task.repository_id}'. "
                f"Available repositories: {list(self.repositories.keys())}"
            )
        self.tasks[task.id] = task

    def load_suite(self, suite_dir: str | Path) -> None:
        """Load repository and task configs from a suite directory."""
        suite_path = Path(suite_dir).resolve()
        if not suite_path.is_dir():
            raise FileNotFoundError(f"Suite directory not found: {suite_path}")

        # 1. Load repositories.yaml
        repo_file = suite_path / "repositories.yaml"
        if repo_file.is_file():
            with open(repo_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for repo_raw in data.get("repositories", []):
                # Resolve path relative to suite_path if relative
                raw_path = repo_raw.get("path", "")
                resolved = str(resolve_path(raw_path, suite_path))
                repo = RepositoryConfig(
                    id=repo_raw["id"],
                    name=repo_raw.get("name", repo_raw["id"]),
                    path=resolved,
                    baseline_commit=repo_raw.get("baseline_commit", "HEAD"),
                    default_test_command=repo_raw.get("default_test_command"),
                    setup_commands=repo_raw.get("setup_commands", []),
                )
                self.register_repository(repo)

        # 2. Load tasks/*.yaml
        tasks_dir = suite_path / "tasks"
        if tasks_dir.is_dir():
            for task_file in sorted(tasks_dir.glob("*.yaml")):
                with open(task_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                for task_raw in data.get("tasks", []):
                    suite_type = TaskSuiteType(task_raw.get("suite", "efficacy"))
                    category = TaskCategory(task_raw.get("category", "bug_fix"))
                    task = BenchmarkTask(
                        id=task_raw["id"],
                        repository_id=task_raw["repository_id"],
                        suite=suite_type,
                        category=category,
                        prompt=task_raw["prompt"],
                        starting_commit=task_raw.get("starting_commit"),
                        target_files=task_raw.get("target_files", []),
                        verification_commands=task_raw.get("verification_commands", []),
                        regression_commands=task_raw.get("regression_commands", []),
                        timeout_seconds=int(task_raw.get("timeout_seconds", 300)),
                        max_iterations=int(task_raw.get("max_iterations", 15)),
                        metadata=task_raw.get("metadata", {}),
                    )
                    self.register_task(task)

    @classmethod
    def create_adhoc(
        cls,
        repo_path: str,
        prompt: str,
        verify_cmd: str,
        regression_cmd: Optional[str] = None,
        starting_commit: Optional[str] = "HEAD",
        suite: TaskSuiteType = TaskSuiteType.EFFICACY,
        category: TaskCategory = TaskCategory.BUG_FIX,
        timeout_seconds: int = 300,
    ) -> TaskRegistry:
        """Create an ad-hoc registry with a single dynamic task and repo."""
        registry = cls()
        abs_path = str(resolve_path(repo_path, Path.cwd()))
        repo_id = Path(abs_path).name or "adhoc_repo"

        repo = RepositoryConfig(
            id=repo_id,
            name=f"Ad-Hoc Repo ({repo_id})",
            path=abs_path,
            baseline_commit=starting_commit,
            default_test_command=verify_cmd,
        )
        registry.register_repository(repo)

        task = BenchmarkTask(
            id=f"{repo_id}_adhoc_01",
            repository_id=repo_id,
            suite=suite,
            category=category,
            prompt=prompt,
            starting_commit=starting_commit,
            verification_commands=[verify_cmd],
            regression_commands=[regression_cmd] if regression_cmd else [],
            timeout_seconds=timeout_seconds,
        )
        registry.register_task(task)
        return registry

    def filter_tasks(
        self,
        task_ids: Optional[List[str]] = None,
        repo_ids: Optional[List[str]] = None,
        suite: Optional[TaskSuiteType] = None,
        category: Optional[TaskCategory] = None,
    ) -> List[BenchmarkTask]:
        """Filter tasks according to specific constraints."""
        filtered = list(self.tasks.values())
        if task_ids:
            filtered = [t for t in filtered if t.id in task_ids]
        if repo_ids:
            filtered = [t for t in filtered if t.repository_id in repo_ids]
        if suite:
            filtered = [t for t in filtered if t.suite == suite]
        if category:
            filtered = [t for t in filtered if t.category == category]
        return filtered

    def validate_environment(self) -> Dict[str, List[str]]:
        """Validate that all registered repositories exist on disk and have git access."""
        issues: Dict[str, List[str]] = {"errors": [], "warnings": []}
        for repo_id, repo in self.repositories.items():
            p = Path(repo.path)
            if not p.exists():
                issues["errors"].append(f"Repository '{repo_id}' path does not exist: {repo.path}")
                continue
            if not (p / ".git").exists():
                issues["warnings"].append(
                    f"Repository '{repo_id}' at {repo.path} is not a git root (.git missing). "
                    f"Will use directory clone fallback."
                )
        return issues
