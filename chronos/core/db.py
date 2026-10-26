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

-- Gate Approvals (F5 Sensitive Module Access Grants)
CREATE TABLE IF NOT EXISTS gate_approvals (
    agent_id             TEXT NOT NULL,
    path_pattern         TEXT NOT NULL,
    approved_by          TEXT NOT NULL DEFAULT 'admin',
    created_at           TEXT NOT NULL,
    expires_at           TEXT,
    PRIMARY KEY (agent_id, path_pattern)
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

-- FTS5 Full-Text Index for AST Code Nodes (Zero-Path Resolution)
CREATE VIRTUAL TABLE IF NOT EXISTS code_nodes_fts USING fts5(
    id UNINDEXED,
    name,
    path,
    signature,
    docstring,
    tokenize='unicode61 remove_diacritics 1'
);

-- Triggers to maintain code_nodes_fts in sync with code_nodes
CREATE TRIGGER IF NOT EXISTS trg_code_nodes_ai AFTER INSERT ON code_nodes BEGIN
    INSERT INTO code_nodes_fts(id, name, path, signature, docstring)
    VALUES (new.id, new.name, new.path, COALESCE(new.signature, ''), COALESCE(new.docstring, ''));
END;

CREATE TRIGGER IF NOT EXISTS trg_code_nodes_ad AFTER DELETE ON code_nodes BEGIN
    DELETE FROM code_nodes_fts WHERE id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_code_nodes_au AFTER UPDATE ON code_nodes BEGIN
    DELETE FROM code_nodes_fts WHERE id = old.id;
    INSERT INTO code_nodes_fts(id, name, path, signature, docstring)
    VALUES (new.id, new.name, new.path, COALESCE(new.signature, ''), COALESCE(new.docstring, ''));
END;

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

-- Epistemic Living Memory (agentmem Integration)
CREATE TABLE IF NOT EXISTS memories (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_namespace      TEXT NOT NULL DEFAULT '',
    tier                TEXT NOT NULL DEFAULT 'learned',
    content             TEXT NOT NULL,
    summary             TEXT NOT NULL DEFAULT '',
    source              TEXT NOT NULL DEFAULT '',
    task_id             TEXT NOT NULL DEFAULT '',
    agent_id            TEXT NOT NULL DEFAULT '',
    outcome             TEXT NOT NULL DEFAULT 'success',
    verification_status TEXT NOT NULL DEFAULT 'unverified',
    confidence          REAL NOT NULL DEFAULT 0.8,
    importance          REAL NOT NULL DEFAULT 0.5,
    evidence            TEXT NOT NULL DEFAULT '',
    tags                TEXT NOT NULL DEFAULT '[]',
    metadata            TEXT NOT NULL DEFAULT '{}',
    supersedes          INTEGER DEFAULT NULL,
    archived            INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_lookup ON memories(repo_namespace, tier, archived);
CREATE INDEX IF NOT EXISTS idx_memories_task ON memories(task_id);
CREATE INDEX IF NOT EXISTS idx_memories_supersedes ON memories(supersedes);

CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    id UNINDEXED,
    content,
    summary,
    tags,
    evidence,
    tokenize='unicode61 remove_diacritics 1'
);

CREATE TRIGGER IF NOT EXISTS trg_memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(id, content, summary, tags, evidence)
    VALUES (new.id, new.content, new.summary, new.tags, new.evidence);
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_memories_au AFTER UPDATE ON memories BEGIN
    DELETE FROM memories_fts WHERE id = old.id;
    INSERT INTO memories_fts(id, content, summary, tags, evidence)
    VALUES (new.id, new.content, new.summary, new.tags, new.evidence);
END;

CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    type       TEXT NOT NULL,
    memory_id  INTEGER NOT NULL,
    FOREIGN KEY (memory_id) REFERENCES memories(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
CREATE INDEX IF NOT EXISTS idx_entities_memory ON entities(memory_id);
"""

_local = threading.local()

def _migrate_memories_schema(con: sqlite3.Connection) -> None:
    """Ensure all expected columns exist on memories table across schema upgrades."""
    try:
        cols = {row[1] for row in con.execute("PRAGMA table_info(memories)").fetchall()}
        if cols:
            expected = {
                "repo_namespace": "TEXT NOT NULL DEFAULT ''",
                "tier": "TEXT NOT NULL DEFAULT 'learned'",
                "summary": "TEXT NOT NULL DEFAULT ''",
                "source": "TEXT NOT NULL DEFAULT ''",
                "task_id": "TEXT NOT NULL DEFAULT ''",
                "agent_id": "TEXT NOT NULL DEFAULT ''",
                "outcome": "TEXT NOT NULL DEFAULT 'success'",
                "verification_status": "TEXT NOT NULL DEFAULT 'unverified'",
                "confidence": "REAL NOT NULL DEFAULT 0.8",
                "importance": "REAL NOT NULL DEFAULT 0.5",
                "evidence": "TEXT NOT NULL DEFAULT ''",
                "tags": "TEXT NOT NULL DEFAULT '[]'",
                "metadata": "TEXT NOT NULL DEFAULT '{}'",
                "supersedes": "INTEGER DEFAULT NULL",
                "archived": "INTEGER NOT NULL DEFAULT 0",
                "created_at": "TEXT NOT NULL DEFAULT ''",
                "updated_at": "TEXT NOT NULL DEFAULT ''",
            }
            for col_name, col_def in expected.items():
                if col_name not in cols:
                    con.execute(f"ALTER TABLE memories ADD COLUMN {col_name} {col_def}")
    except Exception:
        pass

def get_connection(db_path: Union[str, Path] = ":memory:") -> sqlite3.Connection:
    """Returns a configured SQLite connection with WAL mode and robust pragmas."""
    if str(db_path) != ":memory:":
        p = Path(db_path).resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        path_str = str(p)
    else:
        path_str = ":memory:"

    con = sqlite3.connect(path_str, isolation_level=None, check_same_thread=False, timeout=30.0)
    con.row_factory = sqlite3.Row
    
    if path_str != ":memory:":
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA synchronous = NORMAL")
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 30000")
    
    con.executescript(SCHEMA)
    _migrate_memories_schema(con)
    try:
        con.execute(
            """
            INSERT INTO code_nodes_fts(id, name, path, signature, docstring)
            SELECT n.id, n.name, n.path, COALESCE(n.signature, ''), COALESCE(n.docstring, '')
            FROM code_nodes n
            LEFT JOIN code_nodes_fts f ON n.id = f.id
            WHERE f.id IS NULL
            """
        )
    except Exception:
        pass
    try:
        con.execute(
            """
            INSERT INTO memories_fts(id, content, summary, tags, evidence)
            SELECT m.id, m.content, m.summary, m.tags, m.evidence
            FROM memories m
            LEFT JOIN memories_fts f ON m.id = f.id
            WHERE f.id IS NULL
            """
        )
    except Exception:
        pass
    return con
