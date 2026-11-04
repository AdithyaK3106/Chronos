"""`chronos exec`: the sandbox plan, and real enforcement where bubblewrap is available."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from chronos.core.db import get_connection
from chronos.sandbox import plan_sandbox
from chronos.sandbox.runner import build_command


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chronos").mkdir()
    (tmp_path / ".chronos" / "config.json").write_text('{"policy": {}}')
    core = tmp_path / "src" / "app" / "core"
    core.mkdir(parents=True)
    (core / "auth.py").write_text("def login():\n    return 1\n")
    (core / "config.py").write_text("DEBUG = False\n")
    (tmp_path / "src" / "author.py").write_text("NAME = 'x'\n")
    (tmp_path / ".env").write_text("API_KEY=secret\n")
    (tmp_path / ".env.example").write_text("API_KEY=\n")
    (tmp_path / "node_modules" / "auth").mkdir(parents=True)
    (tmp_path / "node_modules" / "auth" / "auth.js").write_text("x")
    return tmp_path


def _rel(plan, paths):
    return {p.relative_to(plan.repo_root).as_posix() for p in paths}


def test_plan_protects_modules_hides_secrets_and_locks_policy(repo):
    plan = plan_sandbox(repo)
    # .env.example is a committed template: neither hidden nor protected
    assert _rel(plan, plan.read_only) == {"src/app/core/auth.py", ".chronos/config.json"}
    assert _rel(plan, plan.hidden) == {".env"}
    # every ancestor of a protected file, including the repo root, is anchored
    assert {".", "src", "src/app", "src/app/core", ".chronos"} <= {
        "." if p == plan.repo_root else p.relative_to(plan.repo_root).as_posix() for p in plan.anchors
    }
    # anchors are bound shallow-first, so deeper binds sit on top of them
    assert [len(p.parts) for p in plan.anchors] == sorted(len(p.parts) for p in plan.anchors)


def test_plan_skips_dependency_trees_and_unprotected_files(repo):
    plan = plan_sandbox(repo)
    everything = _rel(plan, plan.read_only + plan.hidden)
    assert not any(p.startswith("node_modules/") for p in everything)
    assert "src/author.py" not in everything and "src/app/core/config.py" not in everything


def test_gate_approval_leaves_file_writable(repo):
    from chronos.security.gates import grant_gate_approval

    con = get_connection(repo / ".chronos" / "chronos.db")
    grant_gate_approval(con, "claude_code", "src/app/core/auth.py")
    plan = plan_sandbox(repo, agent_id="claude_code", con=con)
    con.close()
    assert "src/app/core/auth.py" in _rel(plan, plan.approved)
    assert "src/app/core/auth.py" not in _rel(plan, plan.read_only)


def test_command_binds_anchors_before_files(repo):
    plan = plan_sandbox(repo)
    argv = build_command(plan, ["claude"])
    assert argv[-2:] == ["--", "claude"]
    first_ro = argv.index("--ro-bind")
    last_anchor = max(i for i, a in enumerate(argv) if a == "--bind" and argv[i + 1] != "/")
    assert last_anchor < first_ro


def _bwrap_works() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    r = subprocess.run(["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "true"], capture_output=True)
    return r.returncode == 0


needs_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap sandbox not available here")


def _exec(repo: Path, script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "chronos.cli.main", "exec", "--repo", str(repo), "--", "bash", "-c", script],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )


ATTACKS = {
    "sed -i": "sed -i '1i # x' src/app/core/auth.py",
    "append": "echo x >> src/app/core/auth.py",
    "rm": "rm -f src/app/core/auth.py",
    "mv over": "echo x > /tmp/chronos_mv_$$ && mv /tmp/chronos_mv_$$ src/app/core/auth.py",
    "rename parent": "mv src/app/core src/app/core2",
    "rename grandparent": "mv src src2",
    "python write": "python3 -c \"open('src/app/core/auth.py','w').write('x')\"",
    "read secret": "cat .env | grep -q secret",
    "edit policy": "echo '{}' > .chronos/config.json",
}


@needs_bwrap
@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_attack_is_blocked(repo, name):
    before = {p: p.read_bytes() for p in (repo / "src/app/core/auth.py", repo / ".env", repo / ".chronos/config.json")}
    r = _exec(repo, ATTACKS[name])
    assert r.returncode != 0, f"{name} succeeded inside the sandbox: {r.stdout} {r.stderr}"
    for path, data in before.items():
        assert path.read_bytes() == data, f"{path} changed on the host"
    assert (repo / "src/app/core").is_dir() and not (repo / "src/app/core2").exists()


@needs_bwrap
def test_normal_work_still_possible(repo):
    r = _exec(repo, "cat src/app/core/auth.py >/dev/null && echo 'DEBUG = True' >> src/app/core/config.py && echo new > src/new.py && [ \"$CHRONOS_SANDBOX\" = 1 ]")
    assert r.returncode == 0, r.stderr
    assert "DEBUG = True" in (repo / "src/app/core/config.py").read_text()
    assert (repo / "src/new.py").read_text() == "new\n"


@needs_bwrap
def test_exit_code_is_propagated(repo):
    assert _exec(repo, "exit 7").returncode == 7


def test_unsupported_platform_refuses_instead_of_running_unprotected(repo, monkeypatch):
    from chronos.sandbox import runner

    monkeypatch.setattr(runner.sys, "platform", "darwin")
    with pytest.raises(runner.SandboxUnavailable):
        runner.run(plan_sandbox(repo), ["true"])
