"""chronos_task_01: impact analysis over (mutually) recursive functions reports each caller once."""
import pytest

from chronos.context.graph import SQLiteGraphStore
from chronos.core.db import get_connection
from chronos.core.models import ASTEdge, ASTNode

T0 = "2026-01-01T00:00:00+00:00"


def _node(name):
    return ASTNode(id=f"m.py::{name}::Function", name=name, path="m.py", kind="Function")


def _calls(a, b):
    return ASTEdge(src=f"m.py::{a}::Function", dst=b, type="CALLS", valid_at=T0)


@pytest.fixture
def store():
    con = get_connection(":memory:")
    s = SQLiteGraphStore(con)
    # is_even <-> is_odd (mutual recursion), walk -> walk (self recursion), main -> is_even, cli -> main
    names = ["is_even", "is_odd", "walk", "main", "cli"]
    s.ingest([_node(n) for n in names], [
        _calls("is_even", "is_odd"), _calls("is_odd", "is_even"),
        _calls("walk", "walk"), _calls("walk", "is_odd"),
        _calls("main", "is_even"), _calls("cli", "main"),
    ])
    yield s
    con.close()


def _depths(rows):
    out = {}
    for r in rows:
        assert r["name"] not in out, f"{r['name']} reported more than once: {rows}"
        out[r["name"]] = r["depth"]
    return out


def test_mutual_recursion_reports_each_caller_once_at_its_shortest_depth(store):
    got = _depths(store.as_of_impact("is_even", max_depth=10))
    assert got == {"is_odd": 1, "main": 1, "walk": 2, "cli": 2}


def test_a_function_is_not_its_own_impact(store):
    assert "walk" not in _depths(store.as_of_impact("walk", max_depth=6))
    assert "is_odd" not in _depths(store.as_of_impact("is_odd", max_depth=6))


def test_max_depth_still_limits_the_traversal(store):
    assert _depths(store.as_of_impact("is_even", max_depth=1)) == {"is_odd": 1, "main": 1}


def test_large_depth_on_a_cycle_terminates_quickly(store):
    import time

    started = time.perf_counter()
    store.as_of_impact("is_even", max_depth=200)
    assert time.perf_counter() - started < 2.0
