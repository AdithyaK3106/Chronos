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
