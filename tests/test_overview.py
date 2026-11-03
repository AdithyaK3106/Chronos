"""Dashboard Overview: every number comes from what Chronos actually recorded."""

from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from chronos.core.db import get_connection
from chronos.core.repo import inspect_repository
from chronos.integrations import claude_code as cc
from chronos.server.overview import build_overview, classify_decision
from chronos.core.paths import audit_log_path
from conftest import write_bench_db


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate user-scope Claude settings from the real ~/.claude."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    return h


@pytest.fixture
def repo(tmp_path, home):
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".git").mkdir()
    (r / "auth.py").write_text("def login():\n    return 1\n")
    (r / "svc.py").write_text('def charge_card(amount):\n    """Charge a customer card."""\n    return amount\n')
    (r / ".env").write_text("TOKEN=abc\n")
    get_connection(r / ".chronos" / "chronos.db").close()
    return r


def _hook(event: str, payload: dict) -> dict | None:
    code, out, _ = cc.run_hook(event, json.dumps(payload))
    assert code == 0
    return json.loads(out) if out else None


def _pre(repo: Path, tool: str, **tool_input) -> dict | None:
    return _hook("pre", {"tool_name": tool, "tool_input": tool_input, "cwd": str(repo), "session_id": "s"})


@pytest.mark.parametrize(
    "record, mode, expected",
    [
        ({"status": "allow"}, "ask", "allowed"),
        ({"status": "deny", "metadata": {"category": "gate"}}, "ask", "asked"),
        ({"status": "deny", "metadata": {"category": "gate"}}, "deny", "denied"),
        ({"status": "deny", "metadata": {"category": "permission"}}, "ask", "denied"),
        ({"status": "deny", "reason": "Path 'auth.py' requires human gate approval under x"}, "ask", "asked"),
        ({"status": "deny", "metadata": {"category": "error"}}, "ask", "error"),
        ({"status": "error"}, "ask", "error"),
    ],
)
def test_classify_decision(record, mode, expected):
    assert classify_decision(record, mode) == expected


def test_overview_reports_what_the_hooks_did(repo):
    cc.install("project", repo, with_mcp=False)
    assert _pre(repo, "Edit", file_path=str(repo / "svc.py"), old_string="a", new_string="b") is None
    assert _pre(repo, "Edit", file_path=str(repo / "auth.py"), old_string="1", new_string="2")["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert _pre(repo, "Read", file_path=str(repo / ".env"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _hook("prompt", {"prompt": "Fix the charge_card function so it rejects negative amounts", "cwd": str(repo), "session_id": "s"})

    ov = build_overview(inspect_repository(repo), probe_hooks=False)

    assert ov["decisions"]["today"] == {"allowed": 1, "asked": 1, "denied": 1, "error": 0}
    assert ov["decisions"]["week"] == ov["decisions"]["today"]
    assert [d["decision"] for d in ov["decisions"]["latest_not_allowed"]] == ["denied", "asked"]

    health = ov["health"]
    assert health["hooks"]["installed"] == ["PreToolUse", "UserPromptSubmit"] and health["hooks"]["complete"]
    assert health["index"]["indexed"] and health["index"]["changed_files"] == 0
    assert health["audit"]["valid"] is True and health["audit"]["records"] == 3

    assert any(p["pattern"] == "*auth*" for p in ov["protection"]["protected"])
    assert "--agent claude_code" in ov["protection"]["grant_command"]

    ctx = ov["context"]
    assert ctx["available"] and ctx["week"]["injections"] == 1 and ctx["week"]["tokens"] > 0
    assert any("svc.py" in f for f in ctx["latest"][0]["files"])

    assert ov["efficiency"]["available"] is False and "benchmarks.cli run" in ov["efficiency"]["empty_hint"]

    # The Advanced security view counts the same events the same way.
    from chronos.server.dashboard_server import aggregate_audit_telemetry

    tel = aggregate_audit_telemetry(audit_log_path(repo))
    assert (tel["denied_count"], tel["held_count"]) == (1, 1)


def test_overview_of_an_untouched_repo_has_hints_not_numbers(tmp_path, home):
    r = tmp_path / "plain"
    r.mkdir()
    (r / "main.py").write_text("print(1)\n")
    ov = build_overview(inspect_repository(r), probe_hooks=False)
    assert ov["health"]["hooks"]["installed"] == [] and "chronos claude install" in ov["health"]["hooks"]["fix"]
    assert ov["health"]["index"]["indexed"] is False and ov["health"]["index"]["fix"]
    assert ov["health"]["audit"]["valid"] is None
    assert ov["decisions"]["available"] is False and ov["decisions"]["empty_hint"]
    assert ov["context"]["available"] is False
    assert ov["efficiency"]["available"] is False
    assert not (r / ".chronos").exists() or not (r / ".chronos" / "chronos.db").exists()


def test_efficiency_is_computed_from_the_benchmark_db_with_its_caveats(repo):
    write_bench_db(repo / "benchmarks" / "data", n_tasks=5)
    eff = build_overview(inspect_repository(repo), probe_hooks=False)["efficiency"]
    assert eff["available"] and eff["pairs"] == 5 and eff["models"] == ["fixture-model"]
    assert eff["token_reduction_pct"] == 60.0
    assert "5 paired runs" in eff["caveat"] and "fixture-model" in eff["caveat"]


def test_overview_endpoint(repo):
    from chronos.cli.ui import register_repo_with_server
    from chronos.server.dashboard_server import create_server

    server = create_server("127.0.0.1", 0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        repo_id = register_repo_with_server("127.0.0.1", port, str(repo))["id"]
        data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{repo_id}/overview?probe=0").read())
        assert set(data) >= {"health", "decisions", "protection", "context", "efficiency"}
    finally:
        server.shutdown()
        server.server_close()


def test_every_command_the_overview_suggests_exists(tmp_path, home):
    """Hints must not send users to commands that do not exist (`chronos index` did not)."""
    import re
    import shlex
    import subprocess
    import sys

    from chronos.server import overview

    source = Path(overview.__file__).read_text(encoding="utf-8")
    commands = set(re.findall(r"`(chronos [^`]+)`", source))
    commands.add(build_overview(inspect_repository(tmp_path), probe_hooks=False)["protection"]["grant_command"])
    assert len(commands) >= 5
    for cmd in sorted(commands):
        args = shlex.split(cmd.replace("<file-or-pattern>", "x"))[1:]
        r = subprocess.run([sys.executable, "-m", "chronos.cli.main", *args, "--help"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, f"{cmd!r}: {r.stderr[-300:]}"
