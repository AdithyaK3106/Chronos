"""chronos_task_03: query_symbols() filters indexed symbols by a regular expression."""
import pytest

from chronos.context.graph import SQLiteGraphStore
from chronos.core.db import get_connection
from chronos.core.models import ASTNode

SYMBOLS = [
    ("auth.py", "login_user", "Function"), ("auth.py", "logout_user", "Function"),
    ("auth.py", "AuthService", "Class"), ("auth.py", "AuthService.refresh_token", "Method"),
    ("db.py", "connect", "Function"), ("db.py", "UserRepo", "Class"), ("db.py", "UserRepo.get_user", "Method"),
]


@pytest.fixture
def store():
    con = get_connection(":memory:")
    s = SQLiteGraphStore(con)
    s.ingest([ASTNode(id=f"{p}::{n}::{k}", name=n, path=p, kind=k) for p, n, k in SYMBOLS], [])
    yield s
    con.close()


def names(rows):
    return [r["name"] for r in rows]


def test_regex_search_on_names_sorted(store):
    assert names(store.query_symbols(r"_user$")) == ["UserRepo.get_user", "login_user", "logout_user"]
    assert names(store.query_symbols(r"^(login|logout)_")) == ["login_user", "logout_user"]
    assert names(store.query_symbols(r"Auth")) == ["AuthService", "AuthService.refresh_token"]


def test_rows_carry_location_and_kind(store):
    (row,) = store.query_symbols(r"^connect$")
    assert row["path"] == "db.py" and row["kind"] == "Function" and row["id"] == "db.py::connect::Function"


def test_kind_filter_and_limit(store):
    assert names(store.query_symbols(r".", kind="Class")) == ["AuthService", "UserRepo"]
    assert len(store.query_symbols(r".", limit=3)) == 3


def test_no_pattern_returns_everything(store):
    assert len(store.query_symbols()) == len(SYMBOLS)


def test_invalid_regex_raises_value_error(store):
    with pytest.raises(ValueError):
        store.query_symbols(r"(unclosed")
