"""Repository identity discovery, canonicalization, and index state detection."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Optional


@dataclass
class RepoIdentity:
    """Resolved identity and indexing state of a codebase repository."""
    id: str
    name: str
    root: str
    canonical_root: str
    is_git: bool
    branch: Optional[str]
    commit: Optional[str]
    is_clean: bool
    has_chronos_dir: bool
    is_indexed: bool
    node_count: int
    edge_count: int
    last_synced_at: Optional[str]
    db_path: str
    audit_log_path: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonicalize_path(path: str | Path) -> Path:
    """Canonicalize a filesystem path resolving symlinks and normalising separators."""
    return Path(path).resolve()


def generate_repo_id(canonical_root: Path) -> str:
    """Generate a stable, unique, and human-readable repository identifier.
    
    Combines a sanitized folder name slug with the first 8 characters of the SHA-256
    hash of the canonical absolute path.
    """
    clean_name = re.sub(r"[^a-zA-Z0-9_-]", "-", canonical_root.name).strip("-") or "project"
    path_bytes = str(canonical_root).lower().encode("utf-8")
    path_hash = hashlib.sha256(path_bytes).hexdigest()[:8]
    return f"{clean_name}-{path_hash}"


def discover_repo_root(start_path: str | Path | None = None) -> Path:
    """Discover the enclosing repository root starting from start_path.
    
    Walks up the directory tree looking for a `.git` or project `.chronos` directory.
    User home directory (~/.chronos) is ignored as a repository marker.
    If neither is found, returns the canonicalized start_path.
    """
    current = canonicalize_path(start_path or os.getcwd())
    user_home = Path.home().resolve()
    
    # Check current directory and parents
    for candidate in [current] + list(current.parents):
        if candidate == user_home and candidate != current:
            continue

        if (candidate / ".git").exists():
            return candidate

        if candidate != user_home and (candidate / ".chronos").is_dir():
            c_dir = candidate / ".chronos"
            if (c_dir / "chronos.db").exists() or (c_dir / "config.json").exists() or (c_dir / "rules").exists():
                return candidate
            
    return current


def get_git_info(repo_root: Path) -> tuple[bool, Optional[str], Optional[str], bool]:
    """Extract git repository information (is_git, branch, commit, is_clean)."""
    git_dir = repo_root / ".git"
    if not git_dir.exists():
        return False, None, None, True

    branch = None
    commit = None
    is_clean = True

    try:
        # Check current branch
        res_b = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_b.returncode == 0:
            branch = res_b.stdout.strip() or None

        # Check commit hash
        res_c = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_c.returncode == 0:
            commit = res_c.stdout.strip() or None

        # Check clean/dirty status
        res_s = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if res_s.returncode == 0:
            is_clean = len(res_s.stdout.strip()) == 0
    except Exception:
        # Fallback to direct reading from .git/HEAD if git executable is not available
        try:
            head_file = git_dir / "HEAD"
            if head_file.is_file():
                content = head_file.read_text(encoding="utf-8").strip()
                if content.startswith("ref: refs/heads/"):
                    branch = content.replace("ref: refs/heads/", "")
                    ref_file = git_dir / "refs" / "heads" / branch
                    if ref_file.is_file():
                        commit = ref_file.read_text(encoding="utf-8").strip()[:7]
                else:
                    commit = content[:7]
        except Exception:
            pass

    return True, branch, commit, is_clean


def inspect_repository(path: str | Path | None = None) -> RepoIdentity:
    """Inspect and resolve complete repository identity, metadata, and index state."""
    repo_root = discover_repo_root(path)
    canonical = canonicalize_path(repo_root)
    repo_id = generate_repo_id(canonical)
    repo_name = canonical.name or "repository"

    is_git, branch, commit, is_clean = get_git_info(canonical)

    chronos_dir = canonical / ".chronos"
    has_chronos_dir = chronos_dir.is_dir()
    db_file = chronos_dir / "chronos.db"
    audit_file = chronos_dir / "chronos-audit.log"

    is_indexed = False
    node_count = 0
    edge_count = 0
    last_synced_at = None

    if db_file.is_file():
        try:
            con = sqlite3.connect(str(db_file), timeout=2.0)
            con.row_factory = sqlite3.Row
            cursor = con.cursor()
            
            # Check if code_nodes table exists
            tables = [r[0] for r in cursor.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            if "code_nodes" in tables:
                r_nodes = cursor.execute("SELECT count(*) as c FROM code_nodes").fetchone()
                node_count = r_nodes["c"] if r_nodes else 0
                
                if "code_edges" in tables:
                    r_edges = cursor.execute("SELECT count(*) as c FROM code_edges").fetchone()
                    edge_count = r_edges["c"] if r_edges else 0

                is_indexed = (node_count > 0)
                
                # Check most recent valid_at in code_edges or db file timestamp
                if is_indexed:
                    if "code_edges" in tables:
                        r_time = cursor.execute("SELECT max(valid_at) as t FROM code_edges").fetchone()
                        if r_time and r_time["t"]:
                            last_synced_at = r_time["t"]

                    if not last_synced_at:
                        mtime = db_file.stat().st_mtime
                        last_synced_at = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            con.close()
        except Exception:
            is_indexed = False

    return RepoIdentity(
        id=repo_id,
        name=repo_name,
        root=str(repo_root),
        canonical_root=str(canonical),
        is_git=is_git,
        branch=branch,
        commit=commit,
        is_clean=is_clean,
        has_chronos_dir=has_chronos_dir,
        is_indexed=is_indexed,
        node_count=node_count,
        edge_count=edge_count,
        last_synced_at=last_synced_at,
        db_path=str(db_file),
        audit_log_path=str(audit_file),
    )
