"""Where Chronos keeps per-user state outside the repository.

The audit log used to live in `<repo>/.chronos/chronos-audit.log`: inside the tree the governed agent
edits, and wiped by `git clean -fdx`. It now lives in the user data directory, one file per
repository, and an existing in-repo log is migrated on first use.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

LEGACY_AUDIT_NAME = "chronos-audit.log"


def data_dir() -> Path:
    """Per-user Chronos data directory. Override with CHRONOS_DATA_DIR."""
    override = os.environ.get("CHRONOS_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "chronos"


def legacy_audit_log_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / ".chronos" / LEGACY_AUDIT_NAME


def audit_log_path(repo_root: str | Path, migrate: bool = True) -> Path:
    """The audit log for `repo_root` in the user data dir (migrating an in-repo log once)."""
    from chronos.core.repo import canonicalize_path, generate_repo_id

    canonical = canonicalize_path(Path(repo_root))
    target = data_dir() / "audit" / f"{generate_repo_id(canonical)}.log"
    if migrate:
        migrate_legacy_audit_log(canonical, target)
    return target


def migrate_legacy_audit_log(repo_root: Path, target: Path) -> str:
    """Move `<repo>/.chronos/chronos-audit.log` to `target` once. Returns what happened.

    Copy, verify the hash chain of the copy, then rename the original to `*.migrated` (kept, not
    deleted). Done under the audit log's own lock so concurrent hooks cannot interleave. If the new
    log already exists, the legacy file is left untouched.
    """
    legacy = legacy_audit_log_path(repo_root)
    if not legacy.is_file() or target.exists():
        return "none"
    from chronos.audit.logger import _InterProcessLock, verify_audit_chain

    target.parent.mkdir(parents=True, exist_ok=True)
    with _InterProcessLock(target):
        if target.exists() or not legacy.is_file():
            return "none"
        tmp = target.with_name(target.name + ".tmp")
        shutil.copy2(legacy, tmp)
        ok_src, _, n_src = verify_audit_chain(legacy)
        ok_dst, _, n_dst = verify_audit_chain(tmp)
        if (ok_src, n_src) != (ok_dst, n_dst):
            tmp.unlink(missing_ok=True)
            return "copy_mismatch"
        os.replace(tmp, target)
        legacy.rename(legacy.with_name(LEGACY_AUDIT_NAME + ".migrated"))
    return "migrated"
