"""Agent identity & API key authentication (F1).

Keys are shown once at creation; only their SHA-256 hash and an 8-char
display prefix are stored. Auth is opt-in: CHRONOS_AUTH=strict is required
for the MCP server to reject unauthenticated calls, so existing installs are
unaffected until an operator turns it on.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from . import audit, db

SYSTEM_AGENT_ID = "chronos-system"
KEY_PREFIX = "chron_"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_hash, key_prefix). Raw key is never stored."""
    raw = KEY_PREFIX + secrets.token_urlsafe(36)[:48]
    return raw, _hash(raw), raw[len(KEY_PREFIX):len(KEY_PREFIX) + 8]


def create_agent(name: str, agent_type: str, owner: str = None,
                 description: str = None, con=None) -> dict:
    c = con or db.get_db()
    raw, key_hash, key_prefix = generate_key()
    agent_id = secrets.token_hex(16)
    c.execute(
        "INSERT INTO agents (agent_id, name, agent_type, key_hash, key_prefix, "
        "status, owner, description, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (agent_id, name, agent_type, key_hash, key_prefix, "active", owner,
         description, _now()))
    return {"agent_id": agent_id, "name": name, "agent_type": agent_type,
            "raw_key": raw, "key_prefix": key_prefix}


def list_agents(con=None) -> list[dict]:
    c = con or db.get_db()
    rows = c.execute(
        "SELECT agent_id, name, agent_type, status, owner, created_at "
        "FROM agents ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_agent(agent_id: str, con=None) -> dict | None:
    c = con or db.get_db()
    r = c.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    return dict(r) if r else None


def rotate_key(agent_id: str, con=None) -> dict:
    c = con or db.get_db()
    if get_agent(agent_id, c) is None:
        return {"rotated": False, "reason": "no such agent"}
    raw, key_hash, key_prefix = generate_key()
    c.execute("UPDATE agents SET key_hash=?, key_prefix=? WHERE agent_id=?",
              (key_hash, key_prefix, agent_id))
    return {"rotated": True, "agent_id": agent_id, "raw_key": raw, "key_prefix": key_prefix}


def suspend_agent(agent_id: str, con=None) -> dict:
    c = con or db.get_db()
    if get_agent(agent_id, c) is None:
        return {"suspended": False, "reason": "no such agent"}
    c.execute("UPDATE agents SET status='suspended' WHERE agent_id=?", (agent_id,))
    return {"suspended": True, "agent_id": agent_id}


def delete_agent(agent_id: str, con=None) -> dict:
    """Soft delete -- historical ledger/audit events must remain intact."""
    c = con or db.get_db()
    if get_agent(agent_id, c) is None:
        return {"deleted": False, "reason": "no such agent"}
    c.execute("UPDATE agents SET status='deleted' WHERE agent_id=?", (agent_id,))
    return {"deleted": True, "agent_id": agent_id}


def resolve_key(raw_key: str | None, con=None) -> dict:
    """Look up the presented key. Always logs to auth_events.

    Returns {"status": "ok"|"invalid_key"|"suspended"|"no_key",
             "agent_id": <id or None>}.
    """
    c = con or db.get_db()
    if not raw_key:
        _record(c, "NO_KEY", None, "no_key")
        return {"status": "no_key", "agent_id": None}
    key_hash = _hash(raw_key)
    prefix = raw_key[len(KEY_PREFIX):len(KEY_PREFIX) + 8] if raw_key.startswith(KEY_PREFIX) else raw_key[:8]
    row = c.execute("SELECT * FROM agents WHERE key_hash=?", (key_hash,)).fetchone()
    if row is None:
        _record(c, prefix, None, "invalid_key")
        return {"status": "invalid_key", "agent_id": None}
    if row["status"] != "active":
        _record(c, prefix, row["agent_id"], "suspended")
        return {"status": "suspended", "agent_id": row["agent_id"]}
    _record(c, prefix, row["agent_id"], "ok")
    return {"status": "ok", "agent_id": row["agent_id"]}


def _record(con, key_prefix, resolved_agent_id, status, ip=None):
    ts = _now()
    con.execute(
        "INSERT INTO auth_events (key_prefix, resolved_agent_id, status, ip, timestamp)"
        " VALUES (?,?,?,?,?)", (key_prefix, resolved_agent_id, status, ip, ts))
    audit.append({"event_type": "auth", "key_prefix": key_prefix,
                  "resolved_agent_id": resolved_agent_id, "status": status,
                  "timestamp": ts})
