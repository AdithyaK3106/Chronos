"""Single SQLite connection manager for all non-graph Chronos state.

Rationale: a design partner doing a self-hosted deployment should be able to
answer "where is Chronos's data?" with one path, and back it up with one file
copy. Locks, provenance, and enforcement rules are one database: chronos.db.

The graph store stays separate and untouched. It is a different engine
(Kuzu/Neo4j) serving a different query pattern, and unifying it would mean
replacing Graphiti -- a wedge-level change, not a packaging one.

AUDIT NOTE (what was actually here before this module existed):
  Only ONE Chronos-owned SQLite file ever existed, not three. ledger.py owned
  it, and rule_store.py already called ledger.connect(), so intent_locks,
  provenance_events and enforcement_rules were already colocated. What this
  module changes is ownership and naming, not physical layout:
    - the file is now chronos.db, named for the product rather than one wedge
    - connections come from one place, so PRAGMAs cannot drift between callers
  The third sqlite3.connect() in the codebase (upstream.py) opens
  codebase-memory-mcp's database read-only. That is a foreign file we consume,
  not Chronos state, and is deliberately NOT consolidated.

ENV VAR NOTE: this uses CHRONOS_SQLITE, not CHRONOS_DB. CHRONOS_DB was already
taken by store.py for the *graph* path; reusing it would have silently pointed
the Kuzu store at a .db file. CHRONOS_LEDGER is still honoured for backward
compatibility with existing installs.
"""

import json
import os
import sqlite3
import threading
from pathlib import Path

_local = threading.local()

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
CREATE TABLE IF NOT EXISTS enforcement_rules (
    rule_id              TEXT PRIMARY KEY,
    language             TEXT NOT NULL,
    rule_text            TEXT NOT NULL DEFAULT '',
    yaml_pattern         TEXT,
    status               TEXT NOT NULL,
    detectability_passed INTEGER NOT NULL DEFAULT 0,
    false_positive_risk  INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    promoted_at          TEXT,
    promoted_by          TEXT
);
"""


def _repo_config_sqlite() -> str | None:
    """`chronos_sqlite` from the active repo's .chronos/config.json, if any.

    Why this lives here and not only in cli.py: every entry point has to agree
    on which database is authoritative, or they silently disagree. `chronos
    enforce` used to be the ONLY caller that read config.json, so rules created
    through the MCP tools landed in the global store while the CLI (and
    therefore CI and the pre-commit hook) read an empty repo-local one and
    reported a clean pass on a blocking violation. Resolution has to be a
    property of the database layer, not of one command.

    Repo is CHRONOS_REPO_PATH, else cwd -- a server started inside a repo is in
    that repo, and one started elsewhere falls through to the global default.
    """
    root = os.environ.get("CHRONOS_REPO_PATH") or "."
    try:
        p = Path(root).resolve() / ".chronos" / "config.json"
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8")).get("chronos_sqlite") or None
    except (OSError, ValueError):
        return None  # unreadable/malformed config must not break the default


def db_path() -> Path:
    """Resolve the SQLite path, identically for every entry point.

    Precedence: CHRONOS_LEDGER (legacy, wins so existing installs need no edit)
    -> CHRONOS_SQLITE -> the active repo's config.json -> the global default."""
    legacy = os.environ.get("CHRONOS_LEDGER")
    p = Path(legacy or os.environ.get("CHRONOS_SQLITE")
             or _repo_config_sqlite()
             or Path.home() / ".chronos" / "chronos.db")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def connect(path=None) -> sqlite3.Connection:
    """A new connection with Chronos's PRAGMAs applied and schema ensured.

    Callers that own a connection (tests, CLI commands) use this. Long-lived
    servers use get_db()."""
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    migrate(target)
    con = sqlite3.connect(str(target), isolation_level=None, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")  # concurrent readers during a write
    con.execute("PRAGMA busy_timeout=5000")
    con.executescript(SCHEMA)
    return con


def get_db(path=None) -> sqlite3.Connection:
    """Thread-local connection, created on first use in each thread.

    sqlite3 connections are not safe to share across threads; the MCP server
    handles tools concurrently, so each thread gets its own."""
    key = str(Path(path) if path else db_path())
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "key", None) == key:
        return cached
    if cached is not None:
        cached.close()  # path changed (tests): drop the stale handle
    _local.conn = connect(path)
    _local.key = key
    return _local.conn


def reset():
    """Drop this thread's cached connection. For tests that switch databases."""
    con = getattr(_local, "conn", None)
    if con is not None:
        con.close()
    _local.conn = None
    _local.key = None


# --- legacy migration ------------------------------------------------------
# Runs once, on first connection, when a pre-unification ledger.db is found
# beside the new path. Copies rows across and renames the old file to .bak, so
# an upgrade needs no manual step and the original remains recoverable.

_LEGACY_NAMES = ("ledger.db", "rule_store.db")
_migrated = set()


def migrate(target: Path) -> dict | None:
    """Copy rows from any legacy SQLite store into `target`. Idempotent."""
    if str(target) in _migrated:
        return None
    _migrated.add(str(target))

    moved = {"locks": 0, "events": 0, "rules": 0}
    found = False
    for name in _LEGACY_NAMES:
        old = target.parent / name
        if not old.exists() or old.resolve() == target.resolve():
            continue
        found = True
        dest = sqlite3.connect(str(target), isolation_level=None)
        dest.executescript(SCHEMA)
        src = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            moved["locks"] += _copy(src, dest, "intent_locks")
            moved["events"] += _copy(src, dest, "provenance_events")
            moved["rules"] += _copy(src, dest, "enforcement_rules")
        finally:
            src.close()
            dest.close()
        old.rename(old.with_suffix(old.suffix + ".bak"))

    if not found:
        return None
    print(f"migrated {moved['locks']} locks, {moved['events']} events, "
          f"{moved['rules']} rules from legacy stores to {target.name}")
    return moved


def _copy(src, dest, table) -> int:
    """Copy one table, skipping rows whose key already exists. INSERT OR IGNORE
    keeps the migration safe to re-run and non-destructive to newer data."""
    try:
        rows = src.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.Error:
        return 0  # legacy file predates this table
    if not rows:
        return 0
    cols = rows[0].keys()
    ph = ",".join("?" * len(cols))
    dest.executemany(
        f"INSERT OR IGNORE INTO {table} ({','.join(cols)}) VALUES ({ph})",
        [tuple(r[c] for c in cols) for r in rows])
    return len(rows)
