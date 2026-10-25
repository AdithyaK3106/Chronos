"""Unified SQLite database connection and schema management."""

import sqlite3
import threading
from pathlib import Path
from typing import Union

SCHEMA = """
-- Intent Locks (Wedge 3)
CREATE TABLE IF NOT EXISTS intent_locks (
    node_id     TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    intent      TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

-- Provenance Events (Wedge 3)
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

-- Agent Identity (F1)
CREATE TABLE IF NOT EXISTS agents (
    agent_id    TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    agent_type  TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    key_prefix  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    owner       TEXT,
    description TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agents_prefix ON agents(key_prefix);

-- Agent Permissions (F2)
CREATE TABLE IF NOT EXISTS agent_permissions (
    agent_id             TEXT PRIMARY KEY,
    allowed_paths        TEXT NOT NULL DEFAULT '["**"]',
    denied_paths         TEXT NOT NULL DEFAULT '[]',
    read_only            INTEGER NOT NULL DEFAULT 0,
    allowed_tools        TEXT NOT NULL DEFAULT '[]',
    max_concurrent_locks INTEGER NOT NULL DEFAULT 10,
    allow_emergency_locks INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

-- Enforcement Rules (Wedge 4)
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

-- Bi-Temporal Code Nodes (Wedge 1 GraphStore)
CREATE TABLE IF NOT EXISTS code_nodes (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    path      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    language  TEXT NOT NULL DEFAULT 'python',
    signature TEXT,
    docstring TEXT
);
CREATE INDEX IF NOT EXISTS idx_code_nodes_name ON code_nodes(name);
CREATE INDEX IF NOT EXISTS idx_code_nodes_path ON code_nodes(path);

-- Bi-Temporal Code Edges (Wedge 1 GraphStore)
CREATE TABLE IF NOT EXISTS code_edges (
    src        TEXT NOT NULL,
    dst        TEXT NOT NULL,
    type       TEXT NOT NULL,
    valid_at   TEXT NOT NULL,
    invalid_at TEXT,
    PRIMARY KEY (src, dst, type, valid_at)
);
CREATE INDEX IF NOT EXISTS idx_code_edges_dst ON code_edges(dst, valid_at, invalid_at);
CREATE INDEX IF NOT EXISTS idx_code_edges_src ON code_edges(src, valid_at, invalid_at);
"""

_local = threading.local()

def get_connection(db_path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    """Returns a configured SQLite connection with WAL mode and robust pragmas."""
    if isinstance(db_path, str) and db_path != ":memory:":
        p = Path(db_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(p)
    else:
        path_str = str(db_path)

    con = sqlite3.connect(path_str, isolation_level=None, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    
    if path_str != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    
    con.executescript(SCHEMA)
    return con
