"""Public benchmark corpus: real issues from repositories we did not write.

`public_v1/tasks.jsonl` lists 50 tasks from SWE-bench Lite (Python) and SWE-bench Multilingual
(JavaScript/TypeScript, Go). Each task pins a repository commit and records:

- `prompt_vague`     a 1-3 sentence description of the problem with no file, path, function or
                     class names (written by Gemini, checked mechanically against the gold answer);
- `prompt_issue`     the original issue text with code blocks, tracebacks, URLs and paths removed;
- `gold_files` / `gold_symbols`   what the real fix changed (source files only, tests excluded);
- `patch`, `test_patch`, `fail_to_pass`   the upstream fix and tests, for solve-rate benchmarks;
- `eval20`           True for the fixed 20-task subset used for quick comparisons.

Repositories are not stored in git. `checkout(task)` fetches the pinned commit into a cache
(`$CHRONOS_BENCH_CACHE`, default `~/.cache/chronos-bench`) on first use:

    from benchmarks.corpus import load_tasks, checkout
    for t in load_tasks(eval20=True):
        repo = checkout(t)   # Path to a clean working tree at t["base_commit"]
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterator

CORPUS_DIR = Path(__file__).parent / "public_v1"
TASKS_FILE = CORPUS_DIR / "tasks.jsonl"


def cache_dir() -> Path:
    return Path(os.environ.get("CHRONOS_BENCH_CACHE") or Path.home() / ".cache" / "chronos-bench")


def load_tasks(eval20: bool | None = None, language: str | None = None, path: Path = TASKS_FILE) -> list[dict[str, Any]]:
    tasks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if eval20 is not None:
        tasks = [t for t in tasks if bool(t.get("eval20")) == eval20]
    if language:
        tasks = [t for t in tasks if t["language"] == language]
    return tasks


def _git(*args: str, cwd: Path | None = None, timeout: int = 600) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[-400:]}")
    return r.stdout


def checkout(task: dict[str, Any], fresh: bool = False) -> Path:
    """A clean working tree of `task["repo"]` at `task["base_commit"]` (fetched once, then cached).

    `fresh=True` discards local changes from a previous run (`git checkout -f` + `git clean -fdx`).
    """
    repo, sha = task["repo"], task["base_commit"]
    mirror = cache_dir() / "repos" / (repo.replace("/", "__") + ".git")
    tree = cache_dir() / "checkouts" / task["instance_id"]
    if not mirror.exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "-q", str(mirror))
        _git("remote", "add", "origin", f"https://github.com/{repo}.git", cwd=mirror)
    if not tree.exists():
        try:
            _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=mirror)
        except RuntimeError:
            _git("fetch", "-q", "--depth", "1", "origin", sha, cwd=mirror, timeout=1800)
        tree.parent.mkdir(parents=True, exist_ok=True)
        _git("--git-dir", str(mirror), "worktree", "add", "-q", "--detach", str(tree), sha)
    elif fresh:
        _git("checkout", "-q", "-f", sha, cwd=tree)
        _git("clean", "-q", "-fdx", cwd=tree)
    return tree


def remove_checkout(task: dict[str, Any]) -> None:
    tree = cache_dir() / "checkouts" / task["instance_id"]
    mirror = cache_dir() / "repos" / (task["repo"].replace("/", "__") + ".git")
    if tree.exists():
        shutil.rmtree(tree, ignore_errors=True)
        subprocess.run(["git", "--git-dir", str(mirror), "worktree", "prune"], capture_output=True)


def iter_checkouts(tasks: list[dict[str, Any]]) -> Iterator[tuple[dict[str, Any], Path]]:
    for t in tasks:
        yield t, checkout(t)
