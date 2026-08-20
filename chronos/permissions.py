"""Per-agent permission scoping (F2).

A manifest is optional: an agent with no row in agent_permissions behaves
exactly as before this feature existed. Enforcement runs from server.py's
_track() wrapper, after auth resolves the calling agent_id.
"""

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import db

WRITE_TOOLS = {
    "chronos_acquire_lock", "chronos_release_lock", "chronos_log_provenance",
    "chronos_capture_lesson", "chronos_propose_rule", "chronos_generate_rule",
    "chronos_promote_rule", "chronos_enforce",
}

_ALLOWED_MANIFEST_KEYS = {
    "allowed_paths", "denied_paths", "read_only", "allowed_tools",
    "max_concurrent_locks", "allow_emergency_locks",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_manifest(path: str) -> dict:
    """Read and validate a permission manifest YAML. Raises ValueError on
    unknown fields or invalid globs."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    unknown = set(raw) - _ALLOWED_MANIFEST_KEYS
    if unknown:
        raise ValueError(f"unknown manifest field(s): {sorted(unknown)}")
    for key in ("allowed_paths", "denied_paths"):
        for pat in raw.get(key) or []:
            if not isinstance(pat, str) or not pat:
                raise ValueError(f"invalid glob in {key}: {pat!r}")
    allowed, denied = raw.get("allowed_paths"), raw.get("denied_paths")
    if allowed and denied and set(allowed) <= set(denied):
        print(f"warning: denied_paths is a superset of allowed_paths -- "
              f"this agent can access nothing")
    return raw


def set_permissions(agent_id: str, manifest: dict, con=None) -> None:
    c = con or db.get_db()
    c.execute("""
        INSERT INTO agent_permissions
            (agent_id, allowed_paths, denied_paths, read_only, allowed_tools,
             max_concurrent_locks, allow_emergency_locks, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(agent_id) DO UPDATE SET
            allowed_paths=excluded.allowed_paths, denied_paths=excluded.denied_paths,
            read_only=excluded.read_only, allowed_tools=excluded.allowed_tools,
            max_concurrent_locks=excluded.max_concurrent_locks,
            allow_emergency_locks=excluded.allow_emergency_locks,
            updated_at=excluded.updated_at
    """, (agent_id,
          json.dumps(manifest.get("allowed_paths")) if manifest.get("allowed_paths") else None,
          json.dumps(manifest.get("denied_paths")) if manifest.get("denied_paths") else None,
          int(bool(manifest.get("read_only"))),
          json.dumps(manifest.get("allowed_tools")) if manifest.get("allowed_tools") else None,
          manifest.get("max_concurrent_locks"),
          int(bool(manifest.get("allow_emergency_locks"))),
          _now()))


def get_permissions(agent_id: str, con=None) -> dict | None:
    c = con or db.get_db()
    r = c.execute("SELECT * FROM agent_permissions WHERE agent_id=?", (agent_id,)).fetchone()
    if r is None:
        return None
    return {
        "allowed_paths": json.loads(r["allowed_paths"]) if r["allowed_paths"] else None,
        "denied_paths": json.loads(r["denied_paths"]) if r["denied_paths"] else None,
        "read_only": bool(r["read_only"]),
        "allowed_tools": json.loads(r["allowed_tools"]) if r["allowed_tools"] else None,
        "max_concurrent_locks": r["max_concurrent_locks"],
        "allow_emergency_locks": bool(r["allow_emergency_locks"]),
    }


def _path_of(node_id: str | None) -> str | None:
    if not node_id:
        return None
    return node_id.split("::", 1)[0]


def check(agent_id: str, tool_name: str, node_id: str | None = None, con=None) -> dict:
    """{"allowed": True} or {"allowed": False, "reason": ..., "rule": ...}."""
    perms = get_permissions(agent_id, con)
    if perms is None:
        return {"allowed": True}

    if perms["allowed_tools"] is not None and tool_name not in perms["allowed_tools"]:
        return {"allowed": False, "reason": "tool_not_allowed",
                "rule": f"allowed_tools does not include {tool_name}"}

    if perms["read_only"] and tool_name in WRITE_TOOLS:
        return {"allowed": False, "reason": "read_only_agent",
                "rule": f"{agent_id} is read_only; {tool_name} is a write operation"}

    path = _path_of(node_id)
    if path:
        denied = perms["denied_paths"] or []
        for pat in denied:
            if fnmatch.fnmatch(path, pat):
                return {"allowed": False, "reason": "path_denied",
                        "rule": f"{path} matches denied_paths pattern {pat!r}"}
        allowed = perms["allowed_paths"]
        if allowed is not None and not any(fnmatch.fnmatch(path, pat) for pat in allowed):
            return {"allowed": False, "reason": "path_not_allowed",
                    "rule": f"{path} does not match any allowed_paths pattern"}

    return {"allowed": True}


def to_yaml(agent_id: str, con=None) -> str:
    perms = get_permissions(agent_id, con)
    if perms is None:
        return "# no permissions manifest set -- all access allowed\n"
    return yaml.safe_dump({k: v for k, v in perms.items() if v is not None}, sort_keys=False)
