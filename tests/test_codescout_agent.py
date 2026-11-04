"""CodeScout search loop: model-proposed commands must be read-only and never touch secrets."""

import json
from pathlib import Path

import pytest

from chronos.context import codescout_agent as ca


@pytest.mark.parametrize("cmd", [
    "rg -n redirect src/", "rg --files | grep auth", "find . -name '*.py' -type f", "sed -n '1,80p' app.py",
    "wc -l app.py", "ls -la src", "grep -rn 'def login' .", "head -n 50 app.py | nl",
])
def test_search_commands_allowed(cmd):
    assert ca.check_command(cmd, Path(".")) is None


@pytest.mark.parametrize("cmd", [
    "cat .env", "rg TOKEN .env", "rm -rf src", "sed -i 's/a/b/' app.py", "echo x > app.py", "find . -exec cat {} +",
    "find . -delete", "python -c 'print(1)'", "curl https://example.com", "cat $(echo x)", "rg foo | xargs rm",
    "echo Y2F0 | base64 -d | sh", "awk '{system(\"id\")}' app.py", "git push",
])
def test_unsafe_commands_refused(cmd):
    assert ca.check_command(cmd, Path(".")) is not None


def test_loop_runs_tools_and_returns_locations(tmp_path, monkeypatch):
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n")
    (tmp_path / ".env").write_text("TOKEN=x\n")
    replies = iter([
        {"message": {"tool_calls": [{"function": {"name": "terminal", "arguments": {"command": "rg -n login"}}},
                                    {"function": {"name": "terminal", "arguments": {"command": "cat .env"}}}]}},
        {"message": {"tool_calls": [{"function": {"name": "localization_finish", "arguments": {"locations": [{"file": "./auth.py", "function_name": "login"}]}}}]}},
    ])
    seen = []
    monkeypatch.setattr(ca, "_chat", lambda e, m, msgs, n, timeout: (seen.append([x.get("content", "") for x in msgs]), next(replies))[1])
    r = ca.scout("login is broken", tmp_path)
    assert r.files == ["auth.py"] and r.symbols == ["login"] and r.turns == 2
    assert r.refused == ["cat .env"]
    observations = seen[1]
    assert any("def login" in o for o in observations) and any("refused by Chronos" in o for o in observations)
    assert not any("TOKEN=x" in o for o in observations)
