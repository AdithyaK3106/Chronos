"""`chronos approve` / `chronos revoke`: gate grant aliases with --agent, --expires and audit events."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from chronos.core.db import get_connection
from chronos.core.paths import audit_log_path
from chronos.security.gates import change_gate_approval, check_gate_approval, is_gate_approved, parse_expires

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src" / "auth").mkdir(parents=True)
    (tmp_path / "src" / "auth" / "login.py").write_text("x = 1\n")
    get_connection(tmp_path / ".chronos" / "chronos.db").close()
    return tmp_path


def _cli(repo, *argv):
    return subprocess.run([sys.executable, "-m", "chronos.cli.main", *argv, "--repo", str(repo)], capture_output=True, text=True)


def _events(repo):
    return [json.loads(line) for line in audit_log_path(repo).read_text().splitlines() if '"gate_approval"' in line]


@pytest.mark.parametrize("value,delta", [("30m", timedelta(minutes=30)), ("2h", timedelta(hours=2)), ("7d", timedelta(days=7)), ("1w", timedelta(weeks=1))])
def test_parse_expires_durations(value, delta):
    assert parse_expires(value, now=NOW) == (NOW + delta).isoformat()


def test_parse_expires_dates_and_errors():
    assert parse_expires(None) is None
    assert parse_expires("2026-12-01", now=NOW) == "2026-12-01T00:00:00+00:00"
    assert parse_expires("2026-12-01T18:00:00+05:30", now=NOW) == "2026-12-01T12:30:00+00:00"
    for bad in ("tomorrow", "0h", "2026-01-01"):
        with pytest.raises(ValueError):
            parse_expires(bad, now=NOW)


def test_approve_then_revoke_via_cli(repo):
    con = get_connection(repo / ".chronos" / "chronos.db")
    assert not check_gate_approval("src/auth/login.py", repo, con=con, agent_id="claude_code")[0]

    r = _cli(repo, "approve", "src/auth/login.py", "--expires", "2h")
    assert r.returncode == 0, r.stderr
    assert "Approved 'src/auth/login.py' for agent 'claude_code' until" in r.stdout
    assert check_gate_approval("src/auth/login.py", repo, con=con, agent_id="claude_code")[0]
    assert not check_gate_approval("src/auth/login.py", repo, con=con, agent_id="other")[0]

    r = _cli(repo, "revoke", "src/auth/login.py")
    assert r.returncode == 0 and "Revoked" in r.stdout
    assert not check_gate_approval("src/auth/login.py", repo, con=con, agent_id="claude_code")[0]
    con.close()

    ev = _events(repo)
    assert [(e["action"], e["status"]) for e in ev] == [("approve", "granted"), ("revoke", "revoked")]
    assert ev[0]["target"] == "src/auth/login.py" and ev[0]["metadata"]["expires_at"]


def test_agent_flag_and_gate_grant_also_audited(repo):
    assert _cli(repo, "approve", "src/auth/*", "--agent", "codex").returncode == 0
    assert _cli(repo, "gate", "grant", "src/auth/*", "--agent", "cursor").returncode == 0
    assert _cli(repo, "gate", "revoke", "src/auth/*", "--agent", "codex").returncode == 0
    assert [(e["agent_id"], e["action"], e["status"]) for e in _events(repo)] == [
        ("codex", "approve", "granted"), ("cursor", "grant", "granted"), ("codex", "revoke", "revoked"),
    ]


def test_revoking_nothing_is_reported_and_bad_expiry_fails(repo):
    r = _cli(repo, "revoke", "nope.py")
    assert r.returncode == 0 and "No approval" in r.stdout
    assert _events(repo)[-1]["status"] == "not_found"
    r = _cli(repo, "approve", "x.py", "--expires", "soon")
    assert r.returncode == 2 and "duration" in r.stderr


def test_expired_grant_stops_applying(repo):
    con = get_connection(repo / ".chronos" / "chronos.db")
    change_gate_approval(con, repo, "approve", "src/auth/login.py", expires="1h")
    assert is_gate_approved(con, "claude_code", "src/auth/login.py")
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    with con:
        con.execute("UPDATE gate_approvals SET expires_at = ?", (past,))
    assert not is_gate_approved(con, "claude_code", "src/auth/login.py")
    con.close()
