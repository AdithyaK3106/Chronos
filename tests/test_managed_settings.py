"""`chronos claude install --managed --out <file>`: an organisation-wide managed-settings.json."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from chronos.integrations import claude_code as cc


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch):
    monkeypatch.setattr(cc, "claude_version", lambda: (2, 1, 300))


def _chronos_hooks(data, event):
    return [h for e in data["hooks"][event] for h in e["hooks"] if cc.is_chronos_hook(h["command"])]


def test_managed_file_has_hooks_fail_closed_and_lock(tmp_path):
    out = tmp_path / "managed-settings.json"
    log = cc.install_managed(out)
    data = json.loads(out.read_text())
    assert data["allowManagedHooksOnly"] is True
    pre = _chronos_hooks(data, "PreToolUse")
    assert len(pre) == 1 and pre[0]["onFailure"] == "block" and pre[0]["command"].endswith("hook claude pre")
    assert data["hooks"]["PreToolUse"][0]["matcher"] == cc.PRE_MATCHER
    assert data["hooks"]["PostToolUse"][0]["matcher"] == cc.POST_MATCHER
    assert _chronos_hooks(data, "UserPromptSubmit")[0]["command"].endswith("hook claude prompt")
    assert cc.pre_hook_fails_closed(data)
    assert any("managed-settings.json" in line for line in log)
    assert not any(line.startswith("warning") for line in log)


def test_managed_merges_and_is_idempotent(tmp_path):
    out = tmp_path / "managed-settings.json"
    other = {"type": "command", "command": "/opt/acme/audit.sh"}
    out.write_text(json.dumps({
        "permissions": {"disableBypassPermissionsMode": "disable"},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [other]}]},
    }))
    cc.install_managed(out)
    cc.install_managed(out)
    data = json.loads(out.read_text())
    assert data["permissions"] == {"disableBypassPermissionsMode": "disable"}
    pre_cmds = [h["command"] for e in data["hooks"]["PreToolUse"] for h in e["hooks"]]
    assert pre_cmds.count("/opt/acme/audit.sh") == 1
    assert len(_chronos_hooks(data, "PreToolUse")) == 1


def test_managed_refuses_invalid_json(tmp_path):
    out = tmp_path / "managed-settings.json"
    out.write_text("{oops")
    with pytest.raises(ValueError, match="not valid JSON"):
        cc.install_managed(out)
    assert out.read_text() == "{oops"


def test_managed_warns_on_old_or_missing_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "claude_version", lambda: (2, 1, 200))
    assert any("2.1.200" in line and "2.1.295" in line for line in cc.install_managed(tmp_path / "a.json"))
    monkeypatch.setattr(cc, "claude_version", lambda: None)
    log = cc.install_managed(tmp_path / "b.json")
    assert any("not found" in line for line in log)
    # the file is fail-closed regardless of the local version
    assert cc.pre_hook_fails_closed(json.loads((tmp_path / "b.json").read_text()))


def test_user_install_warns_on_old_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "claude_version", lambda: (2, 1, 200))
    monkeypatch.delenv("CHRONOS_HOOK_ON_FAILURE", raising=False)
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    log = cc.install(scope="project", repo=tmp_path, with_mcp=False)
    assert any("older than 2.1.295" in line for line in log)
    data = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "onFailure" not in _chronos_hooks(data, "PreToolUse")[0]


def test_cli_managed(tmp_path):
    out = tmp_path / "m.json"
    r = subprocess.run([sys.executable, "-m", "chronos.cli.main", "claude", "install", "--managed", "--out", str(out)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads(out.read_text())["allowManagedHooksOnly"] is True
    assert not (tmp_path / ".claude").exists()  # --managed never touches user/project settings
