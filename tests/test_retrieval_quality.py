"""Resolver fixes found by the retrieval benchmark, and the benchmark's own helpers (no model calls)."""

from __future__ import annotations

import pytest

from benchmarks.retrieval import changed_old_lines, gold_symbols_from_source, strip_paths
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.context.resolver import DeterministicContextResolver
from chronos.core.db import get_connection


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "store").mkdir()
    (tmp_path / "store" / "graph.py").write_text(
        "class GraphStore:\n"
        "    def as_of_impact(self, name):\n"
        "        \"\"\"Transitive callers.\"\"\"\n"
        "        raise NotImplementedError\n\n"
        "class SQLiteGraphStore(GraphStore):\n"
        "    def as_of_impact(self, name):\n"
        "        rows = self.query(name)\n"
        "        return [r for r in rows if r]\n"
    )
    (tmp_path / "server.py").write_text(
        "def as_of_impact(name):\n"
        "    \"\"\"MCP tool wrapper for as_of_impact.\"\"\"\n"
        "    return STORE.as_of_impact(name)\n"
    )
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    nodes, edges = index_repository(tmp_path)
    store = SQLiteGraphStore(con)
    store.sync_repository(nodes, edges)
    yield tmp_path, store
    con.close()


def test_symbol_in_named_file_and_concrete_implementation_win(repo):
    root, store = repo
    res = DeterministicContextResolver(root, store).resolve("Fix as_of_impact() in store/graph.py for recursive code")
    top = res.candidates[0]
    assert top.name == "SQLiteGraphStore.as_of_impact" and top.path.endswith("store/graph.py")
    abstract = next(c for c in res.candidates if c.name == "GraphStore.as_of_impact")
    assert abstract.score < top.score
    assert res.seed_symbols[0] == "SQLiteGraphStore.as_of_impact"  # score order, not alphabetical


def test_member_matching_is_opt_in(repo):
    _, store = repo
    plain = {n["name"] for n in store.get_nodes_by_name("as_of_impact")}
    members = [n["name"] for n in store.get_nodes_by_name("as_of_impact", include_members=True)]
    assert plain == {"as_of_impact"}
    assert members[0] == "as_of_impact" and {"GraphStore.as_of_impact", "SQLiteGraphStore.as_of_impact"} <= set(members)


def test_strip_paths():
    assert strip_paths("Fix as_of_impact() in chronos/context/graph.py for recursion.") == "Fix as_of_impact() for recursion."
    assert strip_paths("Update `src/a.py` and b.json, then test") == "Update and, then test"


def test_gold_symbols_are_innermost_qualified_defs(repo):
    root, _ = repo
    patch = (
        "--- a/store/graph.py\n+++ b/store/graph.py\n"
        "@@ -7,2 +7,3 @@ class SQLiteGraphStore(GraphStore):\n"
        "         rows = self.query(name)\n-        return [r for r in rows if r]\n+        seen = set()\n+        return [r for r in rows if r not in seen]\n"
    )
    assert changed_old_lines(patch) == {"store/graph.py": {8}}
    assert gold_symbols_from_source(root, changed_old_lines(patch)) == ["SQLiteGraphStore.as_of_impact"]
