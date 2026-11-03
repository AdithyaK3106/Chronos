"""The audit log lives in the user data dir, and an in-repo log is migrated once, intact."""

import json
import sys

import pytest

from chronos.audit.logger import append_audit_event, verify_audit_chain
from chronos.core import paths
from chronos.core.paths import audit_log_path, data_dir, legacy_audit_log_path
from chronos.integrations import claude_code as cc


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / ".chronos").mkdir(parents=True)
    (r / ".git").mkdir()
    (r / "app.py").write_text("def run():\n    return 1\n")
    return r


def _legacy_log(repo, n=3):
    log = legacy_audit_log_path(repo)
    for i in range(n):
        append_audit_event(log, "tool_authorization", "claude_code", "file_editor", f"f{i}.py", "allow")
    return log


def test_log_is_outside_the_repository(repo, tmp_path):
    p = audit_log_path(repo)
    assert p.parent == data_dir() / "audit" and p.suffix == ".log"
    assert repo not in p.parents


def test_legacy_log_is_migrated_once_with_its_chain(repo):
    legacy = _legacy_log(repo)
    original = legacy.read_bytes()
    new = audit_log_path(repo)
    assert new.read_bytes() == original
    assert verify_audit_chain(new)[0] and verify_audit_chain(new)[2] == 3
    assert not legacy.exists() and legacy.with_name("chronos-audit.log.migrated").read_bytes() == original
    assert audit_log_path(repo) == new  # second call: nothing to do


def test_hooks_continue_the_migrated_chain(repo):
    _legacy_log(repo)
    payload = {"tool_name": "Edit", "cwd": str(repo),
               "tool_input": {"file_path": str(repo / "app.py"), "old_string": "1", "new_string": "2"}}
    code, out, _ = cc.run_hook("pre", json.dumps(payload))
    assert code == 0 and not out
    ok, _, count = verify_audit_chain(audit_log_path(repo))
    assert ok and count == 4


def test_existing_new_log_wins_and_legacy_is_left_alone(repo):
    new = audit_log_path(repo)
    append_audit_event(new, "tool_authorization", "a", "file_editor", "x.py", "allow")
    legacy = _legacy_log(repo, n=2)
    assert audit_log_path(repo) == new and legacy.exists()
    assert verify_audit_chain(new)[2] == 1


def test_tampered_legacy_log_is_still_migrated_as_evidence(repo):
    legacy = _legacy_log(repo)
    lines = legacy.read_text().splitlines()
    rec = json.loads(lines[1]); rec["target"] = "evil.py"; lines[1] = json.dumps(rec)
    legacy.write_text("\n".join(lines) + "\n")
    new = audit_log_path(repo)
    assert new.exists() and not verify_audit_chain(new)[0]


@pytest.mark.skipif(sys.platform in ("win32", "darwin"), reason="XDG layout")
def test_default_location_follows_xdg(monkeypatch, tmp_path):
    monkeypatch.delenv("CHRONOS_DATA_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert paths.data_dir() == tmp_path / "xdg" / "chronos"
