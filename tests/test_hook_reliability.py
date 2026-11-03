"""Reliability of the Claude Code hooks under real process-level conditions.

Claude Code starts a fresh process for every hook call, and parallel tool calls run hooks
concurrently. These tests use real subprocesses against one repository database.
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from chronos.audit.logger import verify_audit_chain
from chronos.core.paths import audit_log_path

HOOK = [sys.executable, "-m", "chronos.cli.main", "hook", "claude"]


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chronos").mkdir()
    for i in range(30):
        (tmp_path / f"mod_{i}.py").write_text(f"def handler_{i}(x):\n    return helper_{i}(x)\n\ndef helper_{i}(x):\n    return x\n")
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n")
    return tmp_path


def _call(event: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(HOOK + [event], input=json.dumps(payload), capture_output=True, text=True, timeout=60)


def _edit(repo: Path, name: str) -> dict:
    return {"tool_name": "Edit", "tool_input": {"file_path": str(repo / name), "old_string": "x", "new_string": "y"}, "cwd": str(repo)}


def test_concurrent_hooks_keep_the_audit_chain_intact(repo):
    events = [("pre", _edit(repo, f"mod_{i % 30}.py")) for i in range(24)]
    events += [("pre", _edit(repo, "auth.py")) for _ in range(4)]
    events += [("prompt", {"prompt": f"fix handler_{i} so it validates input", "cwd": str(repo)}) for i in range(4)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda e: _call(*e), events))

    assert all(r.returncode == 0 for r in results), [r.stderr[-300:] for r in results if r.returncode]
    # Ordinary edits are allowed silently; any output here means the hook fell back to its error mode.
    assert all(not r.stdout.strip() for r in results[:24]), [(r.stdout + r.stderr)[-400:] for r in results[:24] if r.stdout.strip()]
    decisions = [json.loads(r.stdout)["hookSpecificOutput"].get("permissionDecision") for r in results[24:28]]
    assert decisions == ["ask"] * 4

    ok, msg, count = verify_audit_chain(audit_log_path(repo))
    assert ok, msg
    assert count >= 28  # every pre-tool decision was recorded


def test_pre_hook_latency_budget(repo):
    _call("pre", _edit(repo, "mod_0.py"))  # warm the OS file cache
    times = []
    for i in range(8):
        t = time.perf_counter()
        r = _call("pre", _edit(repo, f"mod_{i}.py"))
        times.append(time.perf_counter() - t)
        assert r.returncode == 0
    p95 = sorted(times)[int(0.95 * (len(times) - 1))]
    print(f"\npre-hook process latency: median {statistics.median(times)*1000:.0f} ms, p95 {p95*1000:.0f} ms")
    # Generous ceiling for CI machines; the product target (150 ms) needs the Phase 5 daemon.
    assert p95 < 2.0


def _decision(r: subprocess.CompletedProcess):
    return json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] if r.stdout.strip() else None


def test_corrupted_database_asks_instead_of_crashing_or_allowing(repo):
    (repo / ".chronos" / "chronos.db").write_bytes(b"this is not a sqlite database" * 100)
    r = _call("pre", _edit(repo, "mod_1.py"))
    assert r.returncode == 0 and _decision(r) == "ask", (r.stdout, r.stderr[-300:])
    # Context injection is a convenience: it fails open and says so on stderr.
    p = _call("prompt", {"prompt": "fix handler_1 so it validates input", "cwd": str(repo)})
    assert p.returncode == 1 and p.stdout == "" and "chronos hook error" in p.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_unwritable_audit_log_asks_instead_of_locking_out(repo):
    import os

    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    log = audit_log_path(repo)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("")
    log.chmod(0o444)
    try:
        r = _call("pre", _edit(repo, "mod_2.py"))
        assert r.returncode == 0 and _decision(r) == "ask", (r.stdout, r.stderr[-300:])
        assert "could not record" in r.stdout
    finally:
        log.chmod(0o644)


def test_killed_mid_index_recovers_cleanly(repo):
    import sqlite3

    for i in range(1500):  # several seconds of indexing, so the kill lands mid-index
        (repo / f"bulk_{i}.py").write_text("".join(f"def f{i}_{j}(a):\n    return g{i}_{j}(a)\n\n" for j in range(15)))
    payload = json.dumps({"prompt": "fix handler_3 so it validates input", "cwd": str(repo)})
    proc = subprocess.Popen(HOOK + ["prompt"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    proc.stdin.write(payload)
    proc.stdin.close()
    time.sleep(1.0)
    proc.kill()
    proc.wait(timeout=10)
    if (repo / ".chronos" / "last_index").exists():
        pytest.skip("indexing finished before the kill; machine too fast for this scenario")

    # The next call must succeed and leave a consistent, fully indexed database.
    r = _call("prompt", json.loads(payload))
    assert r.returncode == 0, r.stderr[-500:]
    con = sqlite3.connect(repo / ".chronos" / "chronos.db")
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    names = {row[0] for row in con.execute("SELECT name FROM code_nodes")}
    con.close()
    assert "handler_3" in names and "f1499_14" in names


def test_db_failure_is_still_audited(repo, monkeypatch):
    """If the Chronos DB cannot be opened, the fallback decision is still recorded in the audit log."""
    from chronos.integrations import claude_code as cc
    import chronos.core.db as db

    def broken(*a, **k):
        raise db.sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_connection", broken)
    code, out, _ = cc.run_hook("pre", json.dumps(_edit(repo, "mod_1.py")))
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
    ok, _, count = verify_audit_chain(audit_log_path(repo))
    assert ok and count == 1
    record = json.loads((audit_log_path(repo)).read_text().splitlines()[0])
    assert record["status"] == "error" and "database is locked" in record["reason"]
