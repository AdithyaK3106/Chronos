"""Group ownership + callable_coverage: the two fixes for silent wrong answers.

Run: pytest tests/test_groups_coverage.py -v

Both bugs these pin were found by pilot agents, not by the suite, because both
produce a confident answer rather than an error.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import coverage, db, groups  # noqa: E402


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONOS_SQLITE", str(tmp_path / "t.db"))
    db.reset()
    c = db.connect()
    yield c
    c.close()
    db.reset()


# --- group ownership -------------------------------------------------------

def test_distinct_repos_get_distinct_groups():
    a = groups.derive(r"C:\p\Setu")
    b = groups.derive(r"C:\p\Shiplog")
    assert a != b and a and b


def test_derive_is_stable():
    """Same repo -> same group, or re-indexing would orphan its own history."""
    assert groups.derive(r"C:\p\Setu") == groups.derive("C:/p/Setu")


def test_literal_default_is_treated_as_unset():
    """'default' is argparse's old value and the cause of the collision bug."""
    g = groups.resolve("default", r"C:\p\Setu")
    assert g == groups.derive(r"C:\p\Setu")


def test_explicit_group_still_wins():
    assert groups.resolve("mine", r"C:\p\Setu") == "mine"


def test_second_repo_is_refused_not_merged(con):
    groups.claim("shared", r"C:\p\A", con)
    with pytest.raises(groups.GroupConflict):
        groups.claim("shared", r"C:\p\B", con)


def test_same_repo_reclaim_is_a_noop(con):
    groups.claim("g", r"C:\p\A", con)
    assert groups.claim("g", r"C:\p\A", con) == "g"  # re-index must not raise


def test_rejection_is_logged_and_queryable(con):
    """A SKIP nobody can read is the silent merge it was meant to replace."""
    groups.claim("g", r"C:\p\A", con)
    with pytest.raises(groups.GroupConflict):
        groups.claim("g", r"C:\p\B", con)
    skips = [r for r in groups.recent(20, con) if r["outcome"] == "SKIP"]
    assert len(skips) == 1
    assert "owned by" in skips[0]["reason"]


def test_release_allows_reclaim(con):
    groups.claim("g", r"C:\p\A", con)
    assert groups.release("g", con) is True
    assert groups.claim("g", r"C:\p\B", con) == "g"
    assert groups.release("nonexistent", con) is False


# --- coverage --------------------------------------------------------------

def _index(path: Path, rows, edges=()):
    """Minimal upstream-shaped index."""
    c = sqlite3.connect(str(path))
    c.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, project TEXT, label TEXT,
            name TEXT, qualified_name TEXT, file_path TEXT, start_line INT,
            end_line INT, properties TEXT);
        CREATE TABLE edges (id INTEGER PRIMARY KEY, project TEXT, source_id INT,
            target_id INT, type TEXT, properties TEXT);""")
    for i, (label, name, fp) in enumerate(rows, 1):
        c.execute("INSERT INTO nodes (id,label,name,file_path,properties) VALUES (?,?,?,?,'')",
                  (i, label, name, fp))
    for s, t in edges:
        c.execute("INSERT INTO edges (source_id,target_id,type) VALUES (?,?,'CALLS')", (s, t))
    c.commit()
    c.close()


def test_dunders_excluded_from_denominator(tmp_path):
    """__init__ has no CALLS edge by construction -- counting it is a false alarm."""
    p = tmp_path / "i.db"
    _index(p, [("Method", "__init__", "lib/a.py"), ("Function", "real", "lib/a.py")],
           edges=[(2, 2)])
    out = coverage.compute(p)
    assert out["callable_total"] == 1, "dunder leaked into the denominator"
    assert out["callable_coverage"] == 1.0


def test_test_functions_and_paths_excluded(tmp_path):
    """A well-tested repo must not score worse than an untested one."""
    p = tmp_path / "i.db"
    _index(p, [("Function", "test_thing", "tests/test_a.py"),
               ("Function", "helper", "tests/conftest.py"),
               ("Function", "real", "lib/a.py")], edges=[(3, 3)])
    out = coverage.compute(p)
    assert out["callable_total"] == 1
    assert out["callable_coverage"] == 1.0


def test_testing_module_is_not_excluded(tmp_path):
    """litestar/testing/ is library code. A blanket '%test%' ate 3650/3683 nodes."""
    p = tmp_path / "i.db"
    _index(p, [("Function", "create_client", "litestar/testing/client.py")])
    out = coverage.compute(p)
    assert out["callable_total"] == 1, "library 'testing' module was wrongly excluded"


def test_low_coverage_flags_error_and_names_di(tmp_path):
    p = tmp_path / "i.db"
    rows = [("Method", f"m{i}", "src/a.service.ts") for i in range(10)]
    rows.append(("Class", "@Injectable", "src/a.service.ts"))
    _index(p, rows, edges=[(1, 1)])
    out = coverage.compute(p)
    assert out["callable_coverage"] < coverage.ERROR_BELOW
    assert out["level"] == "error"
    assert "injection" in out["probable_cause"].lower()
    assert "ERROR" in coverage.warning_line(out)


def test_healthy_coverage_produces_no_warning(tmp_path):
    p = tmp_path / "i.db"
    rows = [("Function", f"f{i}", "lib/a.py") for i in range(10)]
    _index(p, rows, edges=[(i, i) for i in range(1, 9)])
    out = coverage.compute(p)
    assert out["level"] == "ok"
    assert coverage.warning_line(out) is None


def test_manifest_round_trip(tmp_path):
    """doctor reads the manifest rather than rescanning -- it must persist."""
    p = tmp_path / "i.db"
    _index(p, [("Function", "f", "lib/a.py")], edges=[(1, 1)])
    coverage.write_manifest(p, "g", "2026-08-17T00:00:00+00:00")
    m = coverage.read_manifest(p)
    assert m["group_id"] == "g" and m["callable_coverage"] == 1.0


def test_missing_index_degrades(tmp_path):
    out = coverage.compute(tmp_path / "nope.db")
    assert out["callable_coverage"] is None
    assert coverage.warning_line(out) is None
