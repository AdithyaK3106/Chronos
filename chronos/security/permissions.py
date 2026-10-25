"""Least-privilege permission scoping and tool enforcement (F2)."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chronos.core.models import PermissionManifest
from chronos.security.sanitizer import validate_path_access

WRITE_TOOLS = {
    "file_editor",
    "edit_file",
    "write_file",
    "chronos_acquire_lock",
    "chronos_release_lock",
    "chronos_enforce",
}

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def set_permissions(
    con: sqlite3.Connection,
    agent_id: str,
    manifest: PermissionManifest | dict[str, Any],
) -> None:
    """Set or update permission manifest for an agent."""
    if isinstance(manifest, dict):
        manifest_obj = PermissionManifest(**manifest)
    else:
        manifest_obj = manifest

    now = _utc_now_iso()
    with con:
        con.execute(
            """
            INSERT INTO agent_permissions 
                (agent_id, allowed_paths, denied_paths, read_only, allowed_tools, max_concurrent_locks, allow_emergency_locks, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                allowed_paths=excluded.allowed_paths,
                denied_paths=excluded.denied_paths,
                read_only=excluded.read_only,
                allowed_tools=excluded.allowed_tools,
                max_concurrent_locks=excluded.max_concurrent_locks,
                allow_emergency_locks=excluded.allow_emergency_locks,
                updated_at=excluded.updated_at
            """,
            (
                agent_id,
                json.dumps(manifest_obj.allowed_paths),
                json.dumps(manifest_obj.denied_paths),
                1 if manifest_obj.read_only else 0,
                json.dumps(manifest_obj.allowed_tools),
                manifest_obj.max_concurrent_locks,
                1 if manifest_obj.allow_emergency_locks else 0,
                now,
            ),
        )

def get_permissions(con: sqlite3.Connection, agent_id: str) -> PermissionManifest:
    """Get permission manifest for agent, or default permissive manifest if none set."""
    row = con.execute("SELECT * FROM agent_permissions WHERE agent_id = ?", (agent_id,)).fetchone()
    if not row:
        return PermissionManifest()

    return PermissionManifest(
        allowed_paths=json.loads(row["allowed_paths"]),
        denied_paths=json.loads(row["denied_paths"]),
        read_only=bool(row["read_only"]),
        allowed_tools=json.loads(row["allowed_tools"]),
        max_concurrent_locks=row["max_concurrent_locks"],
        allow_emergency_locks=bool(row["allow_emergency_locks"]),
    )

def check_action_allowed(
    con: sqlite3.Connection,
    agent_id: str,
    tool_name: str,
    target_path: str | None,
    repo_root: str | Path,
) -> tuple[bool, str]:
    """Check if agent is permitted to perform this tool action on target_path. Fails closed."""
    try:
        manifest = get_permissions(con, agent_id)

        # 1. Tool authorization check
        if manifest.allowed_tools and tool_name not in manifest.allowed_tools:
            return False, f"Tool '{tool_name}' is not in agent's allowed_tools: {manifest.allowed_tools}"

        # 2. Read-only check for mutating tools
        if manifest.read_only and tool_name in WRITE_TOOLS:
            return False, f"Agent '{agent_id}' is configured read-only and cannot execute write tool '{tool_name}'"

        # 3. Path authorization check if action targets a path
        if target_path:
            is_valid, reason = validate_path_access(
                target_path,
                repo_root=repo_root,
                allowed_globs=manifest.allowed_paths,
                denied_globs=manifest.denied_paths,
            )
            if not is_valid:
                return False, reason

        return True, "Allowed"
    except Exception as e:
        # Strict fail closed
        return False, f"Permission check failed closed due to error: {e}"
