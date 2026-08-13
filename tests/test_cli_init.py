"""`chronos init` — sets a repo up in one step.

pytest (tmp_path). The Claude Desktop config is redirected to a temp file so the
test never touches a real one.
"""

import asyncio
import json
import os
import stat
from types import SimpleNamespace
from unittest.mock import patch

from chronos import cli


def run_init(repo, **env):
    """init against `repo`, with doctor stubbed (it needs a graph and binaries)."""
    args = SimpleNamespace(repo=str(repo), group="test", db=None)

    async def fake_doctor(_a):
        print("doctor: stubbed")

    with patch.object(cli, "do_doctor", fake_doctor):
        with patch.dict(os.environ, env, clear=False):
            asyncio.run(cli.do_init(args))


def test_init_creates_config_and_hooks(tmp_path, capsys):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)          # fake repo
    fake_claude = tmp_path / "claude_desktop_config.json"
    fake_claude.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}),
                           encoding="utf-8")

    with patch.object(cli, "claude_desktop_config", lambda: fake_claude):
        run_init(tmp_path)

    # --- .chronos/ layout ---
    dot = tmp_path / ".chronos"
    assert (dot / "rules").is_dir()
    assert (dot / "logs").is_dir()

    cfg = json.loads((dot / "config.json").read_text(encoding="utf-8"))
    assert cfg["repo"] == str(tmp_path.resolve()), cfg
    assert cfg["chronos_sqlite"] == str(dot / "chronos.db")
    assert cfg["chronos_kuzu_path"] == str(dot / "graph")
    assert cfg["llm_model"] and cfg["auto_triggers"] == "1"

    # --- git hooks exist and are executable ---
    # NTFS has no POSIX exec bit: chmod() cannot set S_IXUSR on Windows, and git
    # runs hooks through its bundled sh regardless. Assert the bit only where it
    # is meaningful, and always assert the shebang, which is what makes the file
    # runnable on either platform.
    for name in ("pre-commit", "post-merge"):
        h = tmp_path / ".git" / "hooks" / name
        assert h.exists(), f"{name} not written"
        if os.name != "nt":
            assert h.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"
        body = h.read_text(encoding="utf-8")
        assert body.startswith("#!/bin/sh")
        assert "\r\n" not in body, "hook must use LF endings or sh will not run it"
    assert "--fail-on-block" in (tmp_path / ".git/hooks/pre-commit").read_text(encoding="utf-8")
    assert "chronos index" in (tmp_path / ".git/hooks/post-merge").read_text(encoding="utf-8")

    # --- MCP block added, other keys untouched ---
    doc = json.loads(fake_claude.read_text(encoding="utf-8"))
    assert doc["mcpServers"]["other"] == {"command": "x"}, "clobbered an unrelated server"
    ch = doc["mcpServers"]["chronos"]
    assert ch["command"] == "python" and ch["args"] == ["-m", "chronos.server"]
    assert ch["env"]["CHRONOS_SQLITE"] == cfg["chronos_sqlite"]
    assert ch["env"]["CHRONOS_AUTO_TRIGGERS"] == "1"

    out = capsys.readouterr().out
    assert "git hooks installed" in out and ".chronos/ created" in out


def test_init_is_idempotent_and_skips_existing_mcp(tmp_path, capsys):
    (tmp_path / ".git" / "hooks").mkdir(parents=True)
    fake_claude = tmp_path / "cfg.json"
    fake_claude.write_text(json.dumps(
        {"mcpServers": {"chronos": {"command": "python", "args": ["-m", "chronos.server"]}}}),
        encoding="utf-8")

    with patch.object(cli, "claude_desktop_config", lambda: fake_claude):
        run_init(tmp_path)
        run_init(tmp_path)  # twice: must not raise

    assert "already present, skipping" in capsys.readouterr().out
    assert (tmp_path / ".chronos" / "config.json").exists()


def test_init_without_git_or_claude_warns_but_succeeds(tmp_path, capsys):
    with patch.object(cli, "claude_desktop_config", lambda: None):
        run_init(tmp_path)          # no .git/, no Claude config

    out = capsys.readouterr().out
    assert "not a git repo" in out
    assert "Claude Desktop config not found" in out
    assert (tmp_path / ".chronos" / "config.json").exists(), "config must still be written"
    assert not (tmp_path / ".git").exists()
