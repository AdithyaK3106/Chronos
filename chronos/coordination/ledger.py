"""Sub-file AST node locking and intent ledger (Wedge 3).

Guarantees:
- Sub-file AST node-level granularity (`path::symbol::kind`).
- Atomic acquisition inside SQLite IMMEDIATE transaction.
- Auto-expiring lease TTL (default 300s) preventing crashed agent deadlocks.
- Same-agent lease renewal.
- Rejection of unauthorized release attempts.
- Append-only provenance recording for every acquire/release/conflict.
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from chronos.core.db import get_connection

DEFAULT_TTL = 300

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

def _iso(dt: datetime) -> str:
    return dt.isoformat()

def acquire_lock(
    con: sqlite3.Connection,
    node_id: str,
    agent_id: str,
    session_id: str,
    intent: str,
    ttl: int = DEFAULT_TTL,
) -> dict[str, Any]:
    """Atomically acquire or renew an AST node lock."""
    now = _utc_now()
    now_str = _iso(now)
    expires_str = _iso(now + timedelta(seconds=ttl))

    # Use IMMEDIATE transaction to prevent race conditions
    con.execute("BEGIN IMMEDIATE")
    try:
        # 1. Sweep expired lock on this node if any
        con.execute(
            "DELETE FROM intent_locks WHERE node_id = ? AND expires_at < ?",
            (node_id, now_str),
        )

        # 2. Check current state
        row = con.execute(
            "SELECT agent_id, session_id, intent, acquired_at, expires_at FROM intent_locks WHERE node_id = ?",
            (node_id,),
        ).fetchone()

        if row is None:
            # Clean acquisition
            con.execute(
                "INSERT INTO intent_locks (node_id, agent_id, session_id, intent, acquired_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, agent_id, session_id, intent, now_str, expires_str),
            )
            con.execute("INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp) VALUES (?, ?, ?, 'acquire', ?, ?)", (node_id, agent_id, session_id, intent, now_str))
            con.execute("COMMIT")
            return {
                "acquired": True,
                "renewed": False,
                "node_id": node_id,
                "agent_id": agent_id,
                "expires_at": expires_str,
            }

        # 3. Existing lock present
        if row["agent_id"] == agent_id:
            con.execute(
                "UPDATE intent_locks SET expires_at = ?, intent = ? WHERE node_id = ?",
                (expires_str, intent, node_id),
            )
            con.execute(
                "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp) VALUES (?, ?, ?, 'renew', ?, ?)",
                (node_id, agent_id, session_id, intent, now_str),
            )
            con.execute("COMMIT")
            return {
                "acquired": True,
                "renewed": True,
                "node_id": node_id,
                "agent_id": agent_id,
                "expires_at": expires_str,
            }

        # 4. Conflict: held by another agent
        con.execute(
            "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp) VALUES (?, ?, ?, 'conflict', ?, ?)",
            (node_id, agent_id, session_id, f"blocked by {row['agent_id']}", now_str),
        )
        con.execute("COMMIT")
        return {
            "acquired": False,
            "renewed": False,
            "reason": "conflict",
            "conflict": {
                "node_id": node_id,
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "intent": row["intent"],
                "expires_at": row["expires_at"],
            },
        }
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise

def release_lock(
    con: sqlite3.Connection,
    node_id: str,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Release a held AST node lock. Only the owning agent may release."""
    now_str = _iso(_utc_now())

    con.execute("BEGIN IMMEDIATE")
    try:
        row = con.execute(
            "SELECT agent_id, session_id, intent FROM intent_locks WHERE node_id = ?",
            (node_id,),
        ).fetchone()

        if row is None:
            con.execute("ROLLBACK")
            return {"released": False, "reason": "not_locked"}

        if row["agent_id"] != agent_id:
            con.execute("ROLLBACK")
            return {
                "released": False,
                "reason": "not_owner",
                "held_by": {"agent_id": row["agent_id"], "session_id": row["session_id"]},
            }

        con.execute("DELETE FROM intent_locks WHERE node_id = ?", (node_id,))
        con.execute(
            "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp) "
            "VALUES (?, ?, ?, 'release', ?, ?)",
            (node_id, agent_id, session_id, row["intent"], now_str),
        )
        con.execute("COMMIT")
        return {"released": True, "node_id": node_id, "intent": row["intent"]}
    except Exception:
        try:
            con.execute("ROLLBACK")
        except Exception:
            pass
        raise

def check_conflicts(con: sqlite3.Connection, node_ids: list[str]) -> dict[str, Any]:
    """Check lock status for a list of nodes, sweeping expired ones."""
    now_str = _iso(_utc_now())
    with con:
        con.execute("DELETE FROM intent_locks WHERE expires_at < ?", (now_str,))
    
    if not node_ids:
        return {"locked": {}, "free": []}

    placeholders = ",".join("?" for _ in node_ids)
    rows = con.execute(
        f"SELECT node_id, agent_id, session_id, intent, expires_at FROM intent_locks WHERE node_id IN ({placeholders})",
        node_ids,
    ).fetchall()

    locked = {
        r["node_id"]: {
            "agent_id": r["agent_id"],
            "session_id": r["session_id"],
            "intent": r["intent"],
            "expires_at": r["expires_at"],
        }
        for r in rows
    }
    free = [nid for nid in node_ids if nid not in locked]
    return {"locked": locked, "free": free}

def list_active_locks(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """List all currently active (non-expired) locks."""
    now_str = _iso(_utc_now())
    with con:
        con.execute("DELETE FROM intent_locks WHERE expires_at < ?", (now_str,))
        rows = con.execute("SELECT * FROM intent_locks ORDER BY acquired_at DESC").fetchall()
    return [dict(r) for r in rows]

def get_provenance(con: sqlite3.Connection, node_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Get the audit provenance history for a specific AST node."""
    rows = con.execute(
        "SELECT * FROM provenance_events WHERE node_id = ? ORDER BY id DESC LIMIT ?",
        (node_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]
