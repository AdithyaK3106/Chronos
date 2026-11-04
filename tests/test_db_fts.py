"""FTS index maintenance must stay linear: hooks open the DB on every tool call."""

from __future__ import annotations

import time
from pathlib import Path

from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.core.db import SCHEMA_VERSION, get_connection

OLD_TRIGGERS = """
DROP TRIGGER trg_code_nodes_ai; DROP TRIGGER trg_code_nodes_ad; DROP TRIGGER trg_code_nodes_au;
CREATE TRIGGER trg_code_nodes_ai AFTER INSERT ON code_nodes BEGIN
    INSERT INTO code_nodes_fts(id, name, path, signature, docstring)
    VALUES (new.id, new.name, new.path, COALESCE(new.signature, ''), COALESCE(new.docstring, ''));
END;
CREATE TRIGGER trg_code_nodes_ad AFTER DELETE ON code_nodes BEGIN
    DELETE FROM code_nodes_fts WHERE id = old.id;
END;
CREATE TRIGGER trg_code_nodes_au AFTER UPDATE ON code_nodes BEGIN
    DELETE FROM code_nodes_fts WHERE id = old.id;
    INSERT INTO code_nodes_fts(id, name, path, signature, docstring)
    VALUES (new.id, new.name, new.path, COALESCE(new.signature, ''), COALESCE(new.docstring, ''));
END;
PRAGMA user_version = 0;
"""


def _write_repo(root: Path, files: int, funcs: int = 15, sig: str = "a") -> None:
    for i in range(files):
        (root / f"bulk_{i}.py").write_text(
            "".join(f"def f{i}_{j}({sig}):\n    return g{i}_{j}(a)\n\n" for j in range(funcs)), encoding="utf-8"
        )


def _fts_hits(con, term: str) -> list[str]:
    return [r[0] for r in con.execute("SELECT id FROM code_nodes_fts WHERE code_nodes_fts MATCH ?", (term,))]


def test_old_id_keyed_database_is_migrated(tmp_path):
    db = tmp_path / "c.db"
    con = get_connection(db)
    con.executescript(OLD_TRIGGERS)
    con.execute("INSERT INTO code_nodes_fts(id, name, path, signature, docstring) VALUES ('junk', 'x', 'x', '', '')")
    _write_repo(tmp_path, 3, funcs=2)
    nodes, edges = index_repository(tmp_path)
    SQLiteGraphStore(con).sync_repository(nodes, edges)  # old triggers: FTS rowids != node rowids
    con.close()

    con = get_connection(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        mismatched = con.execute(
            "SELECT count(*) FROM code_nodes n JOIN code_nodes_fts f ON f.rowid = n.rowid WHERE f.id != n.id"
        ).fetchone()[0]
        assert mismatched == 0
        assert con.execute("SELECT count(*) FROM code_nodes_fts").fetchone()[0] == con.execute(
            "SELECT count(*) FROM code_nodes").fetchone()[0]
        assert len(_fts_hits(con, "f1_1")) == 1

        # Updates and deletes through the new triggers keep exactly one FTS row per node.
        con.execute("UPDATE code_nodes SET docstring = 'refreshed' WHERE name = 'f1_1'")
        assert len(_fts_hits(con, "f1_1")) == 1 and len(_fts_hits(con, "refreshed")) == 1
        con.execute("DELETE FROM code_nodes WHERE name = 'f1_1'")
        assert _fts_hits(con, "f1_1") == []
    finally:
        con.close()


def test_connect_and_full_resync_stay_fast_on_a_large_repo(tmp_path):
    """4500 symbols: connect took 4.6 s and an all-changed re-sync 5.6 s before (quadratic)."""
    _write_repo(tmp_path, 300)
    db = tmp_path / "c.db"
    con = get_connection(db)
    store = SQLiteGraphStore(con)
    nodes, edges = index_repository(tmp_path)
    store.sync_repository(nodes, edges)
    con.close()

    started = time.perf_counter()
    get_connection(db).close()
    assert time.perf_counter() - started < 1.0

    _write_repo(tmp_path, 300, sig="a, b=1")  # every node changes
    con = get_connection(db)
    try:
        nodes, edges = index_repository(tmp_path)
        started = time.perf_counter()
        SQLiteGraphStore(con).sync_repository(nodes, edges)
        assert time.perf_counter() - started < 2.0
        assert len(_fts_hits(con, "f299_14")) == 1
    finally:
        con.close()


V2_SCHEMA = """
CREATE TABLE code_nodes (id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'python', signature TEXT, docstring TEXT);
CREATE VIRTUAL TABLE code_nodes_fts USING fts5(id UNINDEXED, name, path, signature, docstring,
    tokenize='unicode61 remove_diacritics 1');
CREATE TRIGGER trg_code_nodes_ai AFTER INSERT ON code_nodes BEGIN
    INSERT INTO code_nodes_fts(rowid, id, name, path, signature, docstring)
    VALUES (new.rowid, new.id, new.name, new.path, COALESCE(new.signature, ''), COALESCE(new.docstring, ''));
END;
INSERT INTO code_nodes (id, name, path, kind) VALUES ('a.py::check_expiry::Function', 'check_expiry', 'a.py', 'Function');
CREATE TABLE code_edges (src TEXT NOT NULL, dst TEXT NOT NULL, type TEXT NOT NULL, valid_at TEXT NOT NULL,
    invalid_at TEXT, PRIMARY KEY (src, dst, type, valid_at));
INSERT INTO code_edges VALUES ('b.py::caller::Function', 'a.py::check_expiry::Function', 'CALLS', '2026-01-01T00:00:00+00:00', NULL);
INSERT INTO code_edges VALUES ('b.py::caller2::Function', 'check_expiry', 'CALLS', '2026-01-01T00:00:00+00:00', NULL);
PRAGMA user_version = 2;
"""


def test_v2_database_gains_body_terms_and_stemming(tmp_path):
    import sqlite3

    db = tmp_path / ".chronos" / "chronos.db"
    db.parent.mkdir()
    raw = sqlite3.connect(db)
    raw.executescript(V2_SCHEMA)
    raw.close()
    marker = db.parent / "last_index"
    marker.touch()

    con = get_connection(db)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
        assert "terms" in {r[1] for r in con.execute("PRAGMA table_info(code_nodes)")}
        assert "porter" in con.execute("SELECT sql FROM sqlite_master WHERE name='code_nodes_fts'").fetchone()[0]
        assert _fts_hits(con, "check_expiry")  # existing rows were carried over
        assert not marker.exists()  # the hook will re-index and fill in terms
        assert {r[0] for r in con.execute("SELECT dst_name FROM code_edges")} == {"check_expiry"}
        assert "idx_code_edges_dst_name" in {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        # new rows index their body terms, stemmed
        (tmp_path / "svc.py").write_text("def verify(rec):\n    return rec.expires_at < utc_now()\n")
        nodes, edges = index_repository(tmp_path)
        SQLiteGraphStore(con).sync_repository(nodes, edges)
        assert any("verify" in h for h in _fts_hits(con, "expire"))
        assert [r["name"] for r in SQLiteGraphStore(con).query_symbols_lexical(["expiry"])] == ["verify"]
    finally:
        con.close()


def test_body_terms_split_identifiers_and_skip_nested_functions():
    import ast

    from chronos.context.indexer import python_body_terms

    fn = ast.parse(
        "def f(rec):\n    if rec.expiresAt < parse_utc_date('Expired cylinder'):\n        pass\n"
        "    def inner():\n        secret_inner_name()\n"
    ).body[0]
    terms = python_body_terms(fn).split()
    assert {"expires", "parse", "utc", "date", "expired", "cylinder", "rec"} <= set(terms)
    assert "secret" not in terms and "inner" not in terms  # nested defs are indexed as their own nodes
