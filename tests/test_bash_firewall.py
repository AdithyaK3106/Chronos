"""Bash firewall: static pre-check of shell commands and the post-run backstop (no model calls)."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from chronos.core.db import get_connection
from chronos.core.paths import audit_log_path
from chronos.integrations import claude_code as cc
from chronos.security.shell import analyze


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n")
    (tmp_path / "app.py").write_text("def main():\n    return 0\n")
    (tmp_path / ".env").write_text("TOKEN=abc\n")
    (tmp_path / ".env.example").write_text("TOKEN=\n")
    (tmp_path / ".gitignore").write_text(".env\n.chronos/\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True
    )
    get_connection(tmp_path / ".chronos" / "chronos.db").close()
    return tmp_path


def _hook(event, repo, command, tool_use_id="t1"):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo), "session_id": "s", "tool_use_id": tool_use_id}
    code, out, err = cc.run_hook(event, json.dumps(payload))
    assert code == 0, err
    return json.loads(out) if out else None


def _decision(out):
    return out and out["hookSpecificOutput"]["permissionDecision"]


# ----------------------------------------------------------------------------- pre-check
@pytest.mark.parametrize("command", [
    "cat .env",
    "base64 < .env",
    "F=.env; cat $F",
    "python -c \"print(open('.env').read())\"",
    "cat ~/.ssh/id_rsa",
    "grep TOKEN .env | head",
    "echo x > .env",
])
def test_secret_access_is_denied(repo, command):
    assert _decision(_hook("pre", repo, command)) == "deny"


@pytest.mark.parametrize("command", [
    "sed -i 's/1/2/' auth.py",
    "echo 'x = 1' >> auth.py",
    "rm auth.py",
    "mv auth.py old.py",
    "cp app.py auth.py",
    "git checkout -- auth.py",
    "bash -c 'rm -f auth.py'",
    "cd . && perl -pi -e 's/1/2/' auth.py",
    "cat > auth.py <<'EOF'\nx = 1\nEOF",
])
def test_protected_writes_ask(repo, command):
    out = _hook("pre", repo, command)
    assert _decision(out) == "ask"
    assert "auth.py" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("command", [
    "ls -la",
    "ls -la .env",              # metadata only
    "echo .env >> .gitignore",  # names .env as text
    "cat .env.example",         # a template, not a secret
    "sed -i 's/0/1/' app.py",
    "pytest -q > /tmp/out.txt 2>&1",
    "git status && git diff",
    "git checkout -b feature",
    "cat > notes.md <<'EOF'\nsee .env for the variables\nEOF",
])
def test_ordinary_commands_pass(repo, command):
    assert _hook("pre", repo, command) is None


def test_strict_preset_denies_protected_bash_writes(repo):
    (repo / ".chronos" / "config.json").write_text(json.dumps({"policy": {"preset": "strict"}}))
    assert _decision(_hook("pre", repo, "sed -i 's/1/2/' auth.py")) == "deny"


def test_outside_repo_bash_writes_ask_but_temp_is_routine(repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    out = _hook("pre", repo, f"echo x > {elsewhere}/notes.txt")
    # tmp_path_factory lives under the system temp dir, which is exempt
    assert out is None
    out = _hook("pre", repo, "echo x | sudo tee /etc/chronos-test-file")
    assert _decision(out) == "ask" and "outside the repository" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_deny_wins_over_ask(repo):
    out = _hook("pre", repo, "sed -i 's/1/2/' auth.py && cat .env")
    assert _decision(out) == "deny"


def test_secret_denials_are_audited(repo):
    _hook("pre", repo, "cat .env")
    last = json.loads(audit_log_path(repo).read_text().strip().splitlines()[-1])
    assert last["action"] == "bash" and last["status"] == "deny"


# ----------------------------------------------------------------------------- backstop
def test_backstop_flags_protected_change_made_by_a_script(repo):
    """`python fix.py` is opaque to the pre-check; the post check sees what it did."""
    (repo / "fix.py").write_text("open('auth.py', 'a').write('# changed\\n')\n")
    assert _hook("pre", repo, "python fix.py", "u1") is None
    subprocess.run([sys.executable, "fix.py"], cwd=repo, check=True)
    out = _hook("post", repo, "python fix.py", "u1")
    assert out["decision"] == "block"
    assert "auth.py" in out["reason"]
    records = [json.loads(line) for line in audit_log_path(repo).read_text().splitlines()]
    assert any(r["status"] == "violation" and r["target"] == "auth.py" for r in records)


def test_backstop_flags_secret_change(repo):
    assert _hook("pre", repo, "make config", "u2") is None
    (repo / ".env").write_text("TOKEN=changed\n")
    out = _hook("post", repo, "make config", "u2")
    assert out["decision"] == "block" and ".env" in out["reason"]


def test_backstop_ignores_ordinary_and_approved_changes(repo):
    # ordinary file: nothing to report
    assert _hook("pre", repo, "python gen.py", "u3") is None
    (repo / "app.py").write_text("def main():\n    return 2\n")
    assert _hook("post", repo, "python gen.py", "u3") is None
    # a protected write the pre-check asked about (and the user approved) is not flagged again
    assert _decision(_hook("pre", repo, "sed -i 's/1/2/' auth.py", "u4")) == "ask"
    (repo / "auth.py").write_text("def login():\n    return 2\n")
    assert _hook("post", repo, "sed -i 's/1/2/' auth.py", "u4") is None


def test_backstop_respects_gate_grants(repo):
    from chronos.security.gates import grant_gate_approval

    con = get_connection(repo / ".chronos" / "chronos.db")
    try:
        grant_gate_approval(con, "claude_code", "auth.py")
    finally:
        con.close()
    assert _hook("pre", repo, "python fix.py", "u5") is None
    (repo / "auth.py").write_text("x = 1\n")
    assert _hook("post", repo, "python fix.py", "u5") is None


def test_backstop_can_be_disabled(repo):
    (repo / ".chronos" / "config.json").write_text(json.dumps({"policy": {"bash_backstop": False}}))
    assert _hook("pre", repo, "python fix.py", "u6") is None
    (repo / "auth.py").write_text("x = 1\n")
    assert _hook("post", repo, "python fix.py", "u6") is None


def test_post_without_snapshot_is_silent(repo):
    assert _hook("post", repo, "ls", "never-pre") is None


# ----------------------------------------------------------------------------- installer
def test_install_adds_bash_pre_and_post_hooks(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    cc.install(scope="project", repo=tmp_path, with_mcp=False)
    hooks = json.loads((tmp_path / ".claude" / "settings.json").read_text())["hooks"]
    assert "Bash" in hooks["PreToolUse"][0]["matcher"].split("|")
    assert hooks["PostToolUse"][0]["matcher"] == cc.POST_MATCHER
    assert hooks["PostToolUse"][0]["hooks"][0]["command"].endswith("hook claude post")
    assert set(cc.status(scope="project", repo=tmp_path)["hooks_installed"]) == {"PreToolUse", "PostToolUse", "UserPromptSubmit"}
    cc.uninstall(scope="project", repo=tmp_path)
    assert "hooks" not in json.loads((tmp_path / ".claude" / "settings.json").read_text())


# ----------------------------------------------------------------------------- analyzer
def test_analyzer_known_limits_are_opaque_not_silent(tmp_path):
    a = analyze("python fix.py && git reset --hard && make", tmp_path)
    assert {"python", "git reset", "make"} <= set(a.opaque)
    assert a.writes == []


def test_backstop_snapshot_ignores_big_ignored_dirs_but_sees_env(repo):
    """`--untracked-files=all` listed every file of an ignored .venv (53k here: 70 s per Bash call)."""
    import time

    venv = repo / ".venv" / "lib"
    venv.mkdir(parents=True)
    for i in range(3000):
        (venv / f"auth_mod_{i}.py").write_text("x = 1\n")  # protected-looking names, all ignored
    with open(repo / ".gitignore", "a") as fh:
        fh.write(".venv/\n")
    started = time.perf_counter()
    state = cc.sensitive_state(repo)
    assert time.perf_counter() - started < 2.0
    assert ".env" in state
    assert not any(k.startswith(".venv") for k in state)
