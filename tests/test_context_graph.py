"""Tests for AST indexing and bi-temporal graph queries."""

import tempfile
from pathlib import Path

from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_file, index_repository
from chronos.core.models import ASTEdge, ASTNode

SAMPLE_CODE = """
class DatabaseManager:
    \"\"\"Manages database connections.\"\"\"
    def connect(self):
        return True

def query_user(user_id):
    db = DatabaseManager()
    db.connect()
    return {"id": user_id}

def handle_request(req):
    return query_user(req["user_id"])
"""

def test_ast_indexing(tmp_path):
    py_file = tmp_path / "app.py"
    py_file.write_text(SAMPLE_CODE, encoding="utf-8")

    nodes, edges = index_file(py_file)
    node_names = [n.name for n in nodes]

    assert "DatabaseManager" in node_names
    assert "DatabaseManager.connect" in node_names
    assert "query_user" in node_names
    assert "handle_request" in node_names

    edge_pairs = [(e.src, e.dst) for e in edges]
    # query_user calls connect
    assert any("query_user" in src and dst == "connect" for src, dst in edge_pairs)

def test_bitemporal_graph_queries(memory_db):
    store = SQLiteGraphStore(memory_db)

    T0 = "2026-01-01T00:00:00Z"
    T1 = "2026-06-01T00:00:00Z"

    nodes = [
        ASTNode(id="api.py::router::Function", name="router", path="api.py", kind="Function"),
        ASTNode(id="core.py::handler::Function", name="handler", path="core.py", kind="Function"),
        ASTNode(id="legacy.py::old_api::Function", name="old_api", path="legacy.py", kind="Function"),
        ASTNode(id="core.py::new_api::Function", name="new_api", path="core.py", kind="Function"),
    ]

    # At T0, handler called old_api. At T1, it was refactored to call new_api.
    edges = [
        ASTEdge(src="api.py::router::Function", dst="handler", type="CALLS", valid_at=T0),
        ASTEdge(src="core.py::handler::Function", dst="old_api", type="CALLS", valid_at=T0, invalid_at=T1),
        ASTEdge(src="core.py::handler::Function", dst="new_api", type="CALLS", valid_at=T1),
    ]

    store.ingest(nodes, edges)

    # Callers of old_api before refactor (T0)
    callers_before = store.as_of_callers("old_api", as_of="2026-03-01T00:00:00Z")
    assert len(callers_before) == 1
    assert callers_before[0]["name"] == "handler"

    # Callers of old_api after refactor (T1) -> NONE
    callers_after = store.as_of_callers("old_api", as_of="2026-08-01T00:00:00Z")
    assert len(callers_after) == 0

    # Callers of new_api after refactor (T1) -> handler
    new_callers = store.as_of_callers("new_api", as_of="2026-08-01T00:00:00Z")
    assert len(new_callers) == 1
    assert new_callers[0]["name"] == "handler"

    # Diff between T0 and T1
    diff = store.as_of_diff(t1="2026-03-01T00:00:00Z", t2="2026-08-01T00:00:00Z")
    assert len(diff["added_edges"]) == 1
    assert diff["added_edges"][0]["dst"] == "new_api"
    assert len(diff["removed_edges"]) == 1
    assert diff["removed_edges"][0]["dst"] == "old_api"

def test_context_budgeting(memory_db):
    store = SQLiteGraphStore(memory_db)
    nodes = [
        ASTNode(id="node_1", name="auth_service", path="src/auth.py", kind="Function"),
        ASTNode(id="node_2", name="login", path="src/login.py", kind="Function"),
    ]
    edges = [
        ASTEdge(src="node_2", dst="auth_service", type="CALLS", valid_at="2026-01-01T00:00:00Z"),
    ]
    store.ingest(nodes, edges)

    ctx = assemble_context(["auth_service"], store, token_budget=500)
    assert "auth_service" in ctx["text"]
    assert ctx["token_count"] <= 500
    assert not ctx["truncated"]
