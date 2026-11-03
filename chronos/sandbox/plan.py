"""Compute what the sandbox protects: a pure function of the repository and its policy."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

# Never descended into: dependency trees and build output are not policy targets, and .git is
# protected as a whole below.
SKIP_DIRS = {
    ".git", ".chronos", ".venv", "venv", "__pycache__", "node_modules", "dist", "build",
    ".next", ".nuxt", ".turbo", ".output", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "target", "vendor",
}

# Chronos/agent governance files: read-only so the agent cannot switch its guardrails off.
GOVERNANCE_FILES = (
    ".chronos/config.json",
    ".chronos/protected.yml",
    ".claude/settings.json",
)

# Secret files (subset of the firewall's DEFAULT_DENIED_PATTERNS that names files, not trees).
SECRET_PATTERNS = (".env", ".env.*", "*.env", "*.pem", "*.key", "id_rsa*", "credentials*")
SECRET_EXCLUDE = (".env.example", ".env.sample", ".env.template", "*.example", "*.sample")


@dataclass
class SandboxPlan:
    repo_root: Path
    read_only: list[Path] = field(default_factory=list)   # protected modules + governance files
    hidden: list[Path] = field(default_factory=list)      # secrets, made unreadable
    anchors: list[Path] = field(default_factory=list)     # ancestor dirs bound onto themselves
    approved: list[Path] = field(default_factory=list)    # protected but granted: left writable

    def summary(self) -> dict[str, int]:
        return {
            "read_only": len(self.read_only),
            "hidden": len(self.hidden),
            "anchors": len(self.anchors),
            "approved": len(self.approved),
        }


def _is_secret(name: str) -> bool:
    low = name.lower()
    if any(fnmatch.fnmatch(low, ex) for ex in SECRET_EXCLUDE):
        return False
    return any(fnmatch.fnmatch(low, pat) for pat in SECRET_PATTERNS)


def _ancestors(root: Path, path: Path) -> list[Path]:
    """root, root/a, root/a/b ... up to the file's parent (shallow first)."""
    chain = [root]
    for part in path.relative_to(root).parts[:-1]:
        chain.append(chain[-1] / part)
    return chain


def plan_sandbox(repo_root: str | Path, agent_id: str = "claude_code", con=None) -> SandboxPlan:
    """Walk the repository once and decide which paths the sandbox locks down."""
    from chronos.security.gates import find_protected_module, is_gate_approved

    root = Path(repo_root).resolve()
    plan = SandboxPlan(repo_root=root)
    anchors: set[Path] = set()

    def protect(path: Path, hide: bool = False) -> None:
        (plan.hidden if hide else plan.read_only).append(path)
        anchors.update(_ancestors(root, path))

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue  # never bind through a link that may point outside the repo
            rel = path.relative_to(root).as_posix()
            if _is_secret(name):
                protect(path, hide=True)
            elif find_protected_module(rel, root):
                if con is not None and is_gate_approved(con, agent_id, rel):
                    plan.approved.append(path)
                else:
                    protect(path)

    for rel in GOVERNANCE_FILES:
        path = root / rel
        if path.is_file() and not path.is_symlink():
            protect(path)

    plan.anchors = sorted(anchors, key=lambda p: (len(p.parts), str(p)))
    return plan
