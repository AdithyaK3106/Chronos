"""Sensitive module gates: human-in-the-loop lock approval (F5)."""

from datetime import datetime, timezone
import fnmatch
from pathlib import Path
import sqlite3
from typing import Any
import yaml

DEFAULT_PROTECTED_PATTERNS = [
    "*policy*",
    "*auth*",
    "*firewall*",
    "*security*",
    "*.env*",
    "*governor*",
    "*credentials*",
    "*permissions*",
]

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def grant_gate_approval(
    con: sqlite3.Connection,
    agent_id: str,
    path_pattern: str,
    approved_by: str = "admin",
    expires_at: str | None = None,
) -> None:
    """Grant explicit human/admin gate approval for an agent on a protected path pattern."""
    now = _utc_now_iso()
    with con:
        con.execute(
            """
            INSERT INTO gate_approvals (agent_id, path_pattern, approved_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, path_pattern) DO UPDATE SET
                approved_by=excluded.approved_by,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (agent_id, path_pattern, approved_by, now, expires_at),
        )

def revoke_gate_approval(
    con: sqlite3.Connection,
    agent_id: str,
    path_pattern: str,
) -> None:
    """Revoke gate approval for an agent on a path pattern."""
    with con:
        con.execute(
            "DELETE FROM gate_approvals WHERE agent_id = ? AND path_pattern = ?",
            (agent_id, path_pattern),
        )

def is_gate_approved(
    con: sqlite3.Connection | None,
    agent_id: str | None,
    path_str: str,
) -> bool:
    """Check if an active gate approval grant exists for agent on path."""
    if not con or not agent_id:
        return False

    norm = Path(path_str).as_posix()
    now = _utc_now_iso()
    try:
        rows = con.execute(
            """
            SELECT path_pattern FROM gate_approvals
            WHERE agent_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (agent_id, now),
        ).fetchall()
        for r in rows:
            pat = r["path_pattern"] if isinstance(r, sqlite3.Row) else r[0]
            if (
                fnmatch.fnmatch(norm, pat)
                or fnmatch.fnmatch(Path(norm).name, pat)
                or fnmatch.fnmatch(norm, f"**/{pat}")
                or norm == pat
                or norm.endswith(pat.lstrip("/"))
            ):
                return True
        return False
    except Exception:
        # Fail closed on query failure
        return False

def load_protected_modules(repo_root: str | Path) -> list[dict[str, Any]]:
    """Load protected modules from .chronos/protected.yml or fallback to default safety patterns."""
    cfg_file = Path(repo_root).resolve() / ".chronos" / "protected.yml"
    default_modules = [
        {"name": f"default_safety_{pat.strip('*')}", "paths": [f"**/{pat}", f"{pat}", pat]}
        for pat in DEFAULT_PROTECTED_PATTERNS
    ]

    if not cfg_file.is_file():
        # Fail closed: default safety-critical modules protected even without config
        return default_modules

    try:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        custom_modules = data.get("protected_modules", [])
        exempt_patterns = set(data.get("exempt_patterns", []))

        # Merge custom with defaults (unless explicitly exempted)
        active_defaults = [
            m for m in default_modules
            if not any(fnmatch.fnmatch(p, ex) for p in m["paths"] for ex in exempt_patterns)
        ]
        return custom_modules + active_defaults
    except Exception:
        # Fail closed: if config file exists but is corrupted, protect everything
        return [{"name": "corrupted_config_safeguard", "paths": ["**"]}]

def check_gate_approval(
    path_str: str,
    repo_root: str | Path,
    con: sqlite3.Connection | None = None,
    agent_id: str | None = None,
) -> tuple[bool, str]:
    """Check if path falls under a protected module requiring human gate approval."""
    root = Path(repo_root).resolve()
    target = Path(path_str)
    if target.is_absolute():
        try:
            norm = target.resolve().relative_to(root).as_posix()
        except ValueError:
            norm = target.as_posix()
    else:
        try:
            norm = (root / target).resolve().relative_to(root).as_posix()
        except Exception:
            norm = target.as_posix()

    modules = load_protected_modules(root)

    for mod in modules:
        for pat in mod.get("paths", []):
            if (
                fnmatch.fnmatch(norm, pat)
                or fnmatch.fnmatch(Path(norm).name, pat)
                or fnmatch.fnmatch(norm, f"**/{pat}")
            ):
                # Protected pattern matched! Check if explicit gate approval was granted
                if is_gate_approved(con, agent_id, norm):
                    return True, f"Approved under active gate grant for '{norm}'"

                return (
                    False,
                    f"Path '{norm}' requires human gate approval under protected policy '{mod.get('name', pat)}'",
                )

    return True, "Approved"
