"""Intent locks and provenance events (Wedge 3).

Two tables in one SQLite file:

  intent_locks       one row per currently-held node lock, PRIMARY KEY(node_id)
  provenance_events  append-only; who touched what, when, and why

node_id is Wedge 1's identity (upstream's qualified_name), so a lock names the
same thing the temporal graph names -- that is what makes this AST-node-level
rather than file-level.

Concurrency: acquisition is a single INSERT guarded by the primary key inside an
IMMEDIATE transaction. Two agents racing for the same node cannot both win, because
the second INSERT hits the uniqueness constraint rather than a check-then-write gap.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_TTL = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS intent_locks (
    node_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    intent      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provenance_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL,
    agent_id   TEXT NOT NULL,
    session_id TEXT NOT NULL,
    action     TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    timestamp  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prov_node ON provenance_events(node_id, id DESC);
"""


def db_path() -> Path:
    # Unification: the path now comes from db.py, so locks, provenance and
    # enforcement rules share one file (chronos.db) with one set of PRAGMAs.
    # Kept as a function here because callers and tests import it.
    from .db import db_path as _p
    return _p()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat()


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    # Unification: one connection manager (db.py) owns the PRAGMAs, the schema
    # and the legacy migration, so they cannot drift between callers. The
    # ledger's own SCHEMA above is still applied by db.connect().
    from .db import connect as _connect
    return _connect(path)


def sweep_expired(con: sqlite3.Connection) -> int:
    """Drop locks past their TTL. Called before every conflict check, so a crashed
    agent's lock cannot wedge a node forever and no background thread is needed."""
    cur = con.execute("DELETE FROM intent_locks WHERE expires_at <= ?", (_iso(_now()),))
    return cur.rowcount or 0


def _lock_row(r: sqlite3.Row) -> dict:
    return {"node_id": r["node_id"], "agent_id": r["agent_id"], "session_id": r["session_id"],
            "intent": r["intent"], "acquired_at": r["acquired_at"], "expires_at": r["expires_at"]}


def _conflict_hint(node_id, agent_id, held_by, intent):
    """Cross-wedge trigger 3 (informational). Import is local and the call is
    swallowed so a trigger can never affect lock semantics."""
    try:
        from . import triggers
        triggers.on_conflict(node_id, agent_id, held_by, intent)
    except Exception:  # noqa: BLE001 -- best-effort by contract
        pass


def acquire(con, node_id: str, agent_id: str, session_id: str, intent: str,
            ttl_seconds: int = DEFAULT_TTL) -> dict:
    """Take an intent lock on one node.

    Re-acquiring a lock you already hold extends it (agents retry; that should not
    be an error). Any other holder is a conflict, reported with who holds it and
    why so the caller can coordinate instead of guessing.
    """
    if not node_id or not agent_id:
        raise ValueError("node_id and agent_id are required")
    ttl = max(1, int(ttl_seconds))
    now = _now()
    expires = now + timedelta(seconds=ttl)

    con.execute("BEGIN IMMEDIATE")
    try:
        sweep_expired(con)
        row = con.execute("SELECT * FROM intent_locks WHERE node_id = ?", (node_id,)).fetchone()
        if row is not None:
            if row["agent_id"] != agent_id:
                con.execute("ROLLBACK")
                _conflict_hint(node_id, agent_id, row["agent_id"], intent)
                return {"acquired": False, "reason": "conflict", "conflict": _lock_row(row)}
            # same agent -> extend
            con.execute("UPDATE intent_locks SET session_id=?, intent=?, expires_at=? WHERE node_id=?",
                        (session_id, intent, _iso(expires), node_id))
            con.execute("COMMIT")
            return {"acquired": True, "renewed": True, "node_id": node_id,
                    "expires_at": _iso(expires)}
        con.execute(
            "INSERT INTO intent_locks (node_id, agent_id, session_id, intent, acquired_at, expires_at)"
            " VALUES (?,?,?,?,?,?)",
            (node_id, agent_id, session_id, intent, _iso(now), _iso(expires)))
        con.execute("COMMIT")
    except sqlite3.IntegrityError:
        # Lost a race between the SELECT and the INSERT; the other agent won.
        con.execute("ROLLBACK")
        row = con.execute("SELECT * FROM intent_locks WHERE node_id = ?", (node_id,)).fetchone()
        if row is not None:
            _conflict_hint(node_id, agent_id, row["agent_id"], intent)
        return {"acquired": False, "reason": "conflict",
                "conflict": _lock_row(row) if row else None}
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"acquired": True, "renewed": False, "node_id": node_id, "expires_at": _iso(expires)}


def release(con, node_id: str, agent_id: str, session_id: str | None = None) -> dict:
    """Release a lock. Only the holding agent may release it.

    session_id is recorded but not required to match: an agent that crashed and
    reconnected has a new session but is still the same owner.
    """
    row = con.execute("SELECT * FROM intent_locks WHERE node_id = ?", (node_id,)).fetchone()
    if row is None:
        return {"released": False, "reason": "not_locked", "node_id": node_id}
    if row["agent_id"] != agent_id:
        return {"released": False, "reason": "not_owner", "held_by": _lock_row(row)}
    intent = row["intent"]
    con.execute("DELETE FROM intent_locks WHERE node_id = ?", (node_id,))
    return {"released": True, "node_id": node_id, "intent": intent}


def check_conflicts(con, node_ids: list[str]) -> dict:
    """Active locks across a set of nodes -- the pre-flight check before multi-node
    work, so an agent finds out up front instead of halfway through."""
    sweep_expired(con)
    ids = [n for n in dict.fromkeys(node_ids) if n]
    if not ids:
        return {"checked": 0, "locked": [], "free": []}
    marks = ",".join("?" * len(ids))
    rows = con.execute(f"SELECT * FROM intent_locks WHERE node_id IN ({marks})", ids).fetchall()
    locked = [_lock_row(r) for r in rows]
    held = {r["node_id"] for r in rows}
    return {"checked": len(ids), "locked": locked,
            "free": [n for n in ids if n not in held], "conflict_count": len(locked)}


def log_event(con, node_id: str, agent_id: str, session_id: str, action: str,
              reason: str = "") -> dict:
    """Append a provenance event. Append-only: never updated, never deleted."""
    if not node_id or not action:
        raise ValueError("node_id and action are required")
    ts = _iso(_now())
    cur = con.execute(
        "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp)"
        " VALUES (?,?,?,?,?,?)",
        (node_id, agent_id, session_id, action, reason or "", ts))
    return {"logged": True, "id": cur.lastrowid, "node_id": node_id, "timestamp": ts}


def history(con, node_id: str, limit: int = 20) -> dict:
    """Who touched this node, when, and why -- newest first."""
    rows = con.execute(
        "SELECT id, node_id, agent_id, session_id, action, reason, timestamp"
        " FROM provenance_events WHERE node_id = ? ORDER BY id DESC LIMIT ?",
        (node_id, max(1, int(limit)))).fetchall()
    return {"node_id": node_id, "count": len(rows), "events": [dict(r) for r in rows]}


def status(con) -> dict:
    """Ledger health for `chronos doctor`."""
    sweep_expired(con)
    tables = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    return {
        "tables_ok": {"intent_locks", "provenance_events"} <= tables,
        "active_locks": con.execute("SELECT count(*) FROM intent_locks").fetchone()[0],
        "events": con.execute("SELECT count(*) FROM provenance_events").fetchone()[0],
        "path": str(db_path()),
    }
