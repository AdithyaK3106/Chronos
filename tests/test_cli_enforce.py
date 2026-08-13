"""`chronos enforce` CLI layer — the CI integration path.

chronos.enforcer.enforce() is mocked throughout: these tests cover argument
handling, output format and exit codes, not the enforcement engine (which
tests/test_wedge4.py covers against real ast-grep and OPA).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chronos import cli


def result(verdict, rule_id, message, node="getActor"):
    return {"rule_id": rule_id, "verdict": verdict, "matched_node": "x()",
            "matched_qualified_name": node, "deprecated_since": None,
            "rule_status": "blocking", "provenance_event_id": None,
            "message": message}


def run(tmp_path, results, **overrides):
    """enforce on one real .py file, with the engine and driver mocked out."""
    f = tmp_path / "token.py"
    f.write_text("def verify_token():\n    pass\n", encoding="utf-8")

    args = SimpleNamespace(repo=str(tmp_path), file=str(f), diff=None, lang="python",
                           fail_on_block=False, exit_code=False, agent_id=None,
                           session_id=None, group="test", db=None)
    for k, v in overrides.items():
        setattr(args, k, v)

    async def fake_enforce(*a, **k):
        return results

    class Drv:
        async def close(self):
            pass

    with patch("chronos.enforcer.enforce", fake_enforce), \
         patch.object(cli, "open_driver", lambda: Drv()), \
         patch.object(cli, "ensure_schema", lambda d: asyncio.sleep(0)):
        asyncio.run(cli.do_enforce(args))


def test_no_matching_rules_prints_ok(tmp_path, capsys):
    run(tmp_path, [result("pass", "r-1", "no matches")])
    out = capsys.readouterr().out
    assert "OK     token.py" in out or "OK" in out
    assert "Checked 1 files - 0 block, 0 warn, 1 ok" in out


def test_warn_without_exit_code_exits_zero(tmp_path, capsys):
    run(tmp_path, [result("warn", "error-handling-002", "missing error boundary "
                          "[routes.py:18]")])
    out = capsys.readouterr().out
    assert "WARN" in out
    assert "rule:error-handling-002" in out
    assert '"missing error boundary [routes.py:18]"' in out
    assert "Checked 1 files - 0 block, 1 warn, 0 ok" in out
    # no SystemExit == exit 0


def test_block_with_exit_code_exits_one(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        run(tmp_path, [result("block", "verify-token-001",
                              "never call verify_token without is_revoked check "
                              "[token.py:42]")],
            exit_code=True)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "BLOCK" in out and "token.py:42" in out
    assert "rule:verify-token-001" in out
    assert "Checked 1 files - 1 block, 0 warn, 0 ok" in out


def test_block_without_exit_code_exits_zero(tmp_path, capsys):
    """Warn-only runs must not fail the build."""
    run(tmp_path, [result("block", "r-9", "blocked [a.py:1]")])
    assert "1 block" in capsys.readouterr().out


def test_fail_on_block_is_equivalent_to_exit_code(tmp_path):
    with pytest.raises(SystemExit) as e:
        run(tmp_path, [result("block", "r-9", "blocked [a.py:1]")], fail_on_block=True)
    assert e.value.code == 1


def test_repo_config_fills_env_when_unset(tmp_path, monkeypatch):
    import json
    dot = tmp_path / ".chronos"
    dot.mkdir()
    (dot / "config.json").write_text(json.dumps({
        "chronos_sqlite": str(dot / "chronos.db"),
        "chronos_kuzu_path": str(dot / "graph")}), encoding="utf-8")

    monkeypatch.delenv("CHRONOS_SQLITE", raising=False)
    monkeypatch.delenv("CHRONOS_DB", raising=False)
    cli.load_repo_config(str(tmp_path))
    import os
    assert os.environ["CHRONOS_SQLITE"] == str(dot / "chronos.db")
    assert os.environ["CHRONOS_DB"] == str(dot / "graph")


def test_env_wins_over_repo_config(tmp_path, monkeypatch):
    import json
    dot = tmp_path / ".chronos"
    dot.mkdir()
    (dot / "config.json").write_text(json.dumps(
        {"chronos_sqlite": str(dot / "from_config.db")}), encoding="utf-8")

    monkeypatch.setenv("CHRONOS_SQLITE", "/explicit/from_env.db")
    cli.load_repo_config(str(tmp_path))
    import os
    assert os.environ["CHRONOS_SQLITE"] == "/explicit/from_env.db", \
        "an explicitly exported env var must not be overridden by config.json"
