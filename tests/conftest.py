"""Pytest fixtures for Chronos."""

import sqlite3
import tempfile
from pathlib import Path
import pytest

from chronos.core.db import get_connection

@pytest.fixture
def temp_repo(tmp_path):
    """Create a temporary repository directory with .chronos/ setup."""
    chronos_dir = tmp_path / ".chronos"
    chronos_dir.mkdir(parents=True, exist_ok=True)
    db_file = chronos_dir / "chronos.db"
    con = get_connection(db_file)
    yield tmp_path, con, db_file
    con.close()

@pytest.fixture
def memory_db():
    """In-memory SQLite connection with full Chronos schema."""
    con = get_connection(":memory:")
    yield con
    con.close()


# --- Fresh-clone fixtures -------------------------------------------------------------------
# Some tests used to read git-ignored local state (benchmarks/data/benchmark_runs.db, the
# developer's own .chronos index of this checkout), so they failed on a fresh clone / in CI.
# These fixtures build small, deterministic stand-ins inside tmp_path instead.

BENCH_RUNS_SCHEMA = """
CREATE TABLE runs (
    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, repository_id TEXT NOT NULL,
    suite TEXT NOT NULL, arm TEXT NOT NULL, iteration INTEGER NOT NULL, status TEXT NOT NULL,
    duration_seconds REAL NOT NULL, resolved_model_id TEXT NOT NULL, provider_name TEXT NOT NULL,
    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
    tool_calls_count INTEGER NOT NULL, tool_call_types TEXT, error_message TEXT,
    timestamp TEXT NOT NULL, memories_recalled INTEGER DEFAULT 0, memories_stored INTEGER DEFAULT 0
);
CREATE UNIQUE INDEX idx_task_iteration_arm ON runs (task_id, iteration, arm);
"""


def write_bench_db(data_dir: Path, n_tasks: int = 22) -> Path:
    """Write a synthetic benchmark_runs.db with `n_tasks` baseline/treatment pairs.

    Treatment uses 40% of baseline tokens, so token savings are a known 60%.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "benchmark_runs.db"
    con = sqlite3.connect(db_path)
    con.executescript(BENCH_RUNS_SCHEMA)
    for i in range(n_tasks):
        for arm, tokens, secs in (("baseline", 10_000 + i * 100, 60.0 + i), ("treatment", (10_000 + i * 100) * 4 // 10, 40.0 + i)):
            con.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"run-{arm}-{i:02d}", f"task_{i:02d}", "fixture-repo", "efficacy", arm, 1, "success",
                 secs, "fixture-model", "fixture", tokens - 500, 500, tokens, 0.0, 5, "{}", None,
                 f"2026-11-01T10:{i:02d}:00Z", 0, 0),
            )
    con.commit()
    con.close()
    return db_path


@pytest.fixture
def bench_data_dir(tmp_path):
    """A benchmarks/data directory holding a synthetic benchmark_runs.db (22 paired tasks)."""
    data_dir = tmp_path / "bench-repo" / "benchmarks" / "data"
    write_bench_db(data_dir)
    return data_dir


LEDGER_V1 = '''"""Tiny lock ledger used as a fixture."""


def acquire_lock(ledger, path, owner):
    """Acquire a lock on path; prevents race conditions in the lock ledger."""
    if path in ledger:
        return False
    ledger[path] = owner
    return True


def release_lock(ledger, path, owner):
    if ledger.get(path) == owner:
        del ledger[path]
'''

LEDGER_V2 = LEDGER_V1 + '''

def transfer_lock(ledger, path, old, new):
    release_lock(ledger, path, old)
    return acquire_lock(ledger, path, new)
'''


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=Chronos Test", "-c", "user.email=test@example.invalid",
         "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


@pytest.fixture
def ledger_git_repo(tmp_path):
    """A small git repo (two commits) with a lock ledger defining acquire_lock and its callers."""
    repo = tmp_path / "ledger-repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "ledger.py").write_text(LEDGER_V1, encoding="utf-8")
    (repo / "service.py").write_text(
        "from ledger import acquire_lock\n\n\ndef reserve(ledger, path):\n    return acquire_lock(ledger, path, 'svc')\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial ledger")
    (repo / "ledger.py").write_text(LEDGER_V2, encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "add transfer_lock")
    return repo
