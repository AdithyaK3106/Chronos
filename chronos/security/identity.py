"""Agent identity and API key management (F1)."""

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Any

KEY_PREFIX = "chron_"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def hash_key(raw_key: str) -> str:
    """Compute SHA-256 hash of an API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

def generate_key() -> tuple[str, str, str]:
    """Generate (raw_key, key_hash, key_prefix). Raw key is only shown once."""
    raw = KEY_PREFIX + secrets.token_urlsafe(36)[:48]
    return raw, hash_key(raw), raw[:len(KEY_PREFIX) + 8]

def create_agent(
    con: sqlite3.Connection,
    name: str,
    agent_type: str = "assistant",
    owner: str | None = None,
    description: str | None = None,
) -> dict[str, str]:
    """Create an agent with a new API key."""
    raw_key, key_hash, prefix = generate_key()
    agent_id = f"agent_{secrets.token_hex(12)}"
    now = _utc_now_iso()

    with con:
        con.execute(
            "INSERT INTO agents (agent_id, name, agent_type, key_hash, key_prefix, status, owner, description, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (agent_id, name, agent_type, key_hash, prefix, owner, description, now),
        )

    return {
        "agent_id": agent_id,
        "name": name,
        "raw_key": raw_key,
        "key_prefix": prefix,
    }

def authenticate_key(con: sqlite3.Connection, raw_key: str) -> dict[str, Any] | None:
    """Authenticate an agent by raw API key. Returns agent dict or None."""
    if not raw_key.startswith(KEY_PREFIX):
        return None

    prefix = raw_key[:len(KEY_PREFIX) + 8]
    computed_hash = hash_key(raw_key)

    row = con.execute(
        "SELECT agent_id, name, agent_type, status, owner FROM agents WHERE key_prefix = ? AND key_hash = ?",
        (prefix, computed_hash),
    ).fetchone()

    if row and row["status"] == "active":
        return dict(row)
    return None

def get_agent(con: sqlite3.Connection, agent_id: str) -> dict[str, Any] | None:
    row = con.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    return dict(row) if row else None
