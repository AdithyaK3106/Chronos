"""Cross-platform ephemeral workspace manager for chronos-bench."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Optional


class WorktreeManager:
    """Manages isolated, ephemeral git worktrees or directory clones for benchmark runs."""

    def __init__(
        self,
        repo_path: str | Path,
        commit: Optional[str] = "HEAD",
        run_id: Optional[str] = None,
        base_tmp_dir: Optional[str | Path] = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.commit = commit or "HEAD"
        self.run_id = run_id or f"run_{os.urandom(6).hex()}"
        
        # Platform-independent temporary directory
        if base_tmp_dir:
            self.base_tmp_dir = Path(base_tmp_dir).resolve()
        else:
            self.base_tmp_dir = Path(tempfile.gettempdir()) / "chronos_bench_workspaces"
            
        self.workspace_path = self.base_tmp_dir / self.run_id
        self.is_worktree = False

    def setup(self) -> Path:
        """Create the isolated workspace via git worktree or fallback clone."""
        self.base_tmp_dir.mkdir(parents=True, exist_ok=True)
        if self.workspace_path.exists():
            shutil.rmtree(self.workspace_path, ignore_errors=True)

        is_git = (self.repo_path / ".git").exists()

        if is_git:
            try:
                # Attempt git worktree
                cmd = [
                    "git",
                    "-C",
                    str(self.repo_path),
                    "worktree",
                    "add",
                    "--detach",
                    str(self.workspace_path),
                    self.commit,
                ]
                res = subprocess.run(cmd, capture_output=True, text=True, check=True)
                self.is_worktree = True
                return self.workspace_path
            except (subprocess.CalledProcessError, FileNotFoundError):
                # Fallback to local git clone
                self.is_worktree = False

        # Fallback 1: git clone --local
        if is_git:
            try:
                cmd = [
                    "git",
                    "clone",
                    "--local",
                    "--no-hardlinks",
                    str(self.repo_path),
                    str(self.workspace_path),
                ]
                subprocess.run(cmd, capture_output=True, text=True, check=True)
                # Checkout target commit
                subprocess.run(
                    ["git", "-C", str(self.workspace_path), "checkout", self.commit],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                return self.workspace_path
            except (subprocess.CalledProcessError, FileNotFoundError):
                pass

        # Fallback 2: Direct copytree (for non-git or extreme environments)
        shutil.copytree(
            self.repo_path,
            self.workspace_path,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "node_modules"),
        )
        return self.workspace_path

    def get_patch(self) -> str:
        """Extract the git diff patch produced inside the workspace."""
        if not self.workspace_path.exists():
            return ""
        try:
            cmd = ["git", "-C", str(self.workspace_path), "diff", self.commit]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout
            # If commit diff is empty, check uncommitted diff
            cmd = ["git", "-C", str(self.workspace_path), "diff", "HEAD"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return res.stdout if res.returncode == 0 else ""
        except Exception:
            return ""

    def cleanup(self) -> None:
        """Safely remove the workspace and prune git worktrees."""
        if self.is_worktree and self.repo_path.exists():
            try:
                subprocess.run(
                    ["git", "-C", str(self.repo_path), "worktree", "remove", "--force", str(self.workspace_path)],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                subprocess.run(
                    ["git", "-C", str(self.repo_path), "worktree", "prune"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception:
                pass

        if self.workspace_path.exists():
            shutil.rmtree(self.workspace_path, ignore_errors=True)

    def __enter__(self) -> WorktreeManager:
        self.setup()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.cleanup()
