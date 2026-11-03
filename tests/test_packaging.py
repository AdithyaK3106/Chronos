"""Packaging guarantees: the core install works without the OpenHands agent stack."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).parent.parent

# Any import of these raises ImportError inside the child process.
BLOCK_HEAVY = """
import sys
for name in ("openhands", "openhands.sdk", "openhands.tools", "litellm"):
    sys.modules[name] = None
"""


def _run_without_openhands(code: str, stdin: str = "", cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", BLOCK_HEAVY + code],
        input=stdin, capture_output=True, text=True, cwd=str(cwd or ROOT), timeout=120,
    )


def test_core_dependencies_exclude_the_agent_stack():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    core = " ".join(project["dependencies"]).lower()
    assert "openhands" not in core and "litellm" not in core
    assert any("openhands-sdk" in d for d in project["optional-dependencies"]["agent"])


def test_core_entry_points_import_without_openhands():
    r = _run_without_openhands(
        "import chronos, chronos.cli.main, chronos.integrations.claude_code, "
        "chronos.governance.firewall, chronos.context.session, chronos.server.mcp_server, "
        "chronos.server.dashboard_server\nprint('ok')"
    )
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip().endswith("ok")


def test_claude_hook_runs_without_openhands(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n")
    (tmp_path / ".chronos").mkdir()
    event = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "auth.py"), "old_string": "1", "new_string": "2"}, "cwd": str(tmp_path)})
    r = _run_without_openhands(
        "import sys\nfrom chronos.integrations.claude_code import run_hook\n"
        "code, out, err = run_hook('pre', sys.stdin.read())\nprint(out)\nsys.exit(code)",
        stdin=event, cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr[-2000:]
    # Protected module: the default policy asks the developer instead of hard-denying.
    assert json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_runtime_aliases_still_resolve():
    import chronos.context.session as session
    import chronos.governance.firewall as firewall
    import runtime.hooks
    import runtime.session

    assert runtime.hooks is firewall and runtime.session is session
