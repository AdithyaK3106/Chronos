"""chronos_task_05: one reusable connection context manager instead of manual open/close pairs."""
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from chronos.core.db import connection

ROOT = Path.cwd()


def test_yields_a_configured_connection_and_closes_it(tmp_path):
    with connection(tmp_path / "c.db") as con:
        assert con.execute("SELECT count(*) FROM sqlite_master WHERE name = 'code_nodes'").fetchone()[0] == 1
        assert con.row_factory is sqlite3.Row
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


def test_closes_even_when_the_body_raises(tmp_path):
    with pytest.raises(RuntimeError):
        with connection(tmp_path / "c.db") as con:
            raise RuntimeError("boom")
    with pytest.raises(sqlite3.ProgrammingError):
        con.execute("SELECT 1")


@pytest.mark.parametrize("rel", ["chronos/cli/main.py", "chronos/server/mcp_server.py"])
def test_callers_no_longer_close_connections_by_hand(rel):
    source = (ROOT / rel).read_text(encoding="utf-8")
    assert "con.close()" not in source
    assert "connection(" in source


def test_cli_init_and_sync_still_work(tmp_path):
    (tmp_path / "app.py").write_text("def handler(x):\n    return helper(x)\n\ndef helper(x):\n    return x\n")
    for cmd in ("init", "sync"):
        r = subprocess.run([sys.executable, "-m", "chronos.cli.main", cmd, "--repo", str(tmp_path)],
                           capture_output=True, text=True, timeout=120, cwd=ROOT)
        assert r.returncode == 0, r.stdout + r.stderr
    con = sqlite3.connect(tmp_path / ".chronos" / "chronos.db")
    assert {"handler", "helper"} <= {row[0] for row in con.execute("SELECT name FROM code_nodes")}
    con.close()
