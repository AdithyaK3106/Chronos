"""Tests for append-only SHA-256 hash-chained audit logging."""

import json
from pathlib import Path

from chronos.audit.logger import append_audit_event, verify_audit_chain, GENESIS_HASH

def test_hash_chain_integrity(tmp_path):
    log_file = tmp_path / "chronos-audit.log"

    # Write 3 audit events
    e1 = append_audit_event(log_file, "tool_authorization", "agent_1", "read_file", "src/main.py", "allow")
    e2 = append_audit_event(log_file, "tool_authorization", "agent_1", "write_file", "src/main.py", "allow")
    e3 = append_audit_event(log_file, "tool_authorization", "agent_2", "edit_file", "src/secret.env", "deny", reason="restricted")

    assert e1["prev_hash"] == GENESIS_HASH
    assert e2["prev_hash"] == e1["hash"]
    assert e3["prev_hash"] == e2["hash"]

    # Verify chain
    is_valid, msg, count = verify_audit_chain(log_file)
    assert is_valid is True
    assert count == 3

def test_tamper_detection(tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    append_audit_event(log_file, "action", "agent_1", "edit", "file1.py", "allow")
    append_audit_event(log_file, "action", "agent_1", "edit", "file2.py", "allow")

    # Tamper with the first line
    lines = log_file.read_text(encoding="utf-8").splitlines()
    tampered_entry = json.loads(lines[0])
    tampered_entry["action"] = "malicious_overwrite"
    lines[0] = json.dumps(tampered_entry)
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Verification must detect the tampering
    is_valid, msg, _ = verify_audit_chain(log_file)
    assert is_valid is False
    assert "Broken hash chain" in msg or "Hash mismatch" in msg

def test_audit_scrubs_secrets(tmp_path):
    log_file = tmp_path / "chronos-audit.log"
    append_audit_event(
        log_file,
        event_type="auth",
        agent_id="agent_1",
        action="login",
        target="chron_secretkey1234567890abcdef",
        status="allow",
        reason="Bearer secret_token_xyz12345",
        metadata={"token": "sk-12345678901234567890"},
    )

    content = log_file.read_text(encoding="utf-8")
    assert "chron_[REDACTED]" in content
    assert "chron_secretkey" not in content
    assert "sk-[REDACTED]" in content
