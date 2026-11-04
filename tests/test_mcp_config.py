"""install.sh must merge the Chronos MCP server into editor configs, never overwrite them."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from chronos.integrations.mcp_config import ConfigError, add_server, remove_server

ROOT = Path(__file__).resolve().parent.parent
EXISTING = {
    "mcpServers": {"github": {"command": "gh-mcp", "args": ["--stdio"]}, "postgres": {"command": "pg-mcp", "env": {"DB": "x"}}},
    "theme": "dark",
}


def test_add_keeps_other_servers_and_settings(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(EXISTING))
    msg = add_server(p, "/usr/bin/chronos", ["serve"])
    data = json.loads(p.read_text())
    assert data["mcpServers"]["github"] == EXISTING["mcpServers"]["github"]
    assert data["mcpServers"]["postgres"] == EXISTING["mcpServers"]["postgres"]
    assert data["theme"] == "dark"
    assert data["mcpServers"]["chronos"] == {"command": "/usr/bin/chronos", "args": ["serve"]}
    assert "kept 2 other server(s)" in msg
    assert json.loads((tmp_path / "mcp.json.chronos-bak").read_text()) == EXISTING


def test_add_is_idempotent_and_backup_is_the_original(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(EXISTING))
    add_server(p, "/a/chronos")
    add_server(p, "/b/chronos")
    assert "already configured" in add_server(p, "/b/chronos")
    assert json.loads((tmp_path / "mcp.json.chronos-bak").read_text()) == EXISTING
    assert json.loads(p.read_text())["mcpServers"]["chronos"]["command"] == "/b/chronos"


def test_missing_or_empty_file_is_created(tmp_path):
    p = tmp_path / "new" / "mcp.json"
    add_server(p, "chronos")
    assert json.loads(p.read_text()) == {"mcpServers": {"chronos": {"command": "chronos", "args": ["serve"]}}}
    e = tmp_path / "empty.json"
    e.write_text("  \n")
    add_server(e, "chronos")
    assert "chronos" in json.loads(e.read_text())["mcpServers"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"mcpServers": []}'])
def test_unparseable_config_is_left_untouched(tmp_path, content):
    p = tmp_path / "mcp.json"
    p.write_text(content)
    with pytest.raises(ConfigError):
        add_server(p, "chronos")
    assert p.read_text() == content
    assert not (tmp_path / "mcp.json.chronos-bak").exists()


def test_remove_only_removes_chronos(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(EXISTING))
    add_server(p, "chronos")
    remove_server(p)
    assert json.loads(p.read_text()) == EXISTING


def test_cli_mcp_config_add(tmp_path):
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps(EXISTING))
    r = subprocess.run([sys.executable, "-m", "chronos.cli.main", "mcp-config", "add", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    data = json.loads(p.read_text())
    assert set(data["mcpServers"]) == {"github", "postgres", "chronos"} and data["mcpServers"]["chronos"]["args"][-1] == "serve"


def test_cli_mcp_config_invalid_json_exits_nonzero(tmp_path):
    """install.sh picks its warning branch from the exit code, so a refused file must exit 1."""
    p = tmp_path / "mcp.json"
    p.write_text("{not json")
    r = subprocess.run([sys.executable, "-m", "chronos.cli.main", "mcp-config", "add", str(p)], capture_output=True, text=True)
    assert r.returncode == 1 and "not valid JSON" in r.stderr
    assert p.read_text() == "{not json"


def _installer_mcp_step() -> str:
    text = (ROOT / "install.sh").read_text()
    start = text.index("# Step 6: IDE FastMCP Configuration")
    end = text.index("# Step 7:")
    return text[start:end]


@pytest.mark.skipif(sys.platform == "win32", reason="install.sh is POSIX")
def test_installer_keeps_existing_mcp_servers(tmp_path):
    """Run install.sh's real MCP step against a home directory that already has MCP servers."""
    home = tmp_path / "home"
    cursor = home / ".cursor" / "mcp.json"
    desktop = home / ".config" / "Claude" / "claude_desktop_config.json"
    for f in (cursor, desktop):
        f.parent.mkdir(parents=True)
        f.write_text(json.dumps(EXISTING))
    shim = tmp_path / "chronos"
    shim.write_text(f"#!/bin/sh\nexec {sys.executable} -m chronos.cli.main \"$@\"\n")
    shim.chmod(0o755)
    script = "\n".join([
        'GREEN=""; YELLOW=""; RESET=""; BOLD=""; OS="Linux"; CONFIGURE_MCP=true',
        f'CHRONOS_BIN="{shim}"',
        _installer_mcp_step(),
    ])
    env = {**os.environ, "HOME": str(home), "PYTHONPATH": str(ROOT)}
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    for f in (cursor, desktop):
        data = json.loads(f.read_text())
        assert {"github", "postgres", "chronos"} <= set(data["mcpServers"]), (f, data)
        assert data["theme"] == "dark"
    assert "kept 2 other server(s)" in r.stdout


def test_installer_never_overwrites_with_a_heredoc():
    assert not re.search(r"cat\s+<<\s*'?EOF'?\s*>\s*\"?\$(CURSOR_MCP|CLAUDE_CONFIG)", (ROOT / "install.sh").read_text())
