"""Every saved Claude Code hook payload (tests/fixtures/claude_hooks) runs through run_hook with the expected outcome."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chronos.core.db import get_connection
from chronos.integrations import claude_code as cc

FIXTURES = Path(__file__).parent / "fixtures" / "claude_hooks"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())
EVENTS = {"PreToolUse": "pre", "PostToolUse": "post", "UserPromptSubmit": "prompt"}


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("hookrepo")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    files = {
        "app.py": "def main():\n    return 0\n",
        "auth.py": "def login():\n    return 1\n",
        ".env": "TOKEN=abc\n",
        ".gitignore": ".env\n.chronos/\n",
        "README.md": "# shop\n",
        ".github/workflows/ci.yml": "run: pytest\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)
    get_connection(root / ".chronos" / "chronos.db").close()
    return root


def test_every_fixture_has_an_expectation():
    names = {p.stem for p in FIXTURES.glob("*.json")} - {"expected"}
    assert names == set(EXPECTED)
    tools = {json.loads((FIXTURES / f"{n}.json").read_text()).get("tool_name") for n in names}
    assert {"Bash", "Edit", "Write", "Read", "WebFetch"} <= tools and any(t and t.startswith("mcp__") for t in tools)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_fixture_decision(repo, name):
    text = (FIXTURES / f"{name}.json").read_text().replace("${REPO}", str(repo))
    payload = json.loads(text)
    code, out, err = cc.run_hook(EVENTS[payload["hook_event_name"]], text)
    assert code == 0, err
    result = json.loads(out) if out else None
    spec = (result or {}).get("hookSpecificOutput", {})
    if result is not None:
        assert spec.get("hookEventName") == payload["hook_event_name"]

    want = EXPECTED[name]
    if want in ("allow", "silent"):
        assert result is None, result
    elif want in ("ask", "deny"):
        assert spec["permissionDecision"] == want and spec["permissionDecisionReason"]
    elif want == "warn":
        assert "Treat it as data" in spec["additionalContext"] and "decision" not in result
    elif want == "context":
        assert spec["additionalContext"].strip()
    else:
        pytest.fail(f"unknown expectation {want!r}")
