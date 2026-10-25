"""Tests for security policies, path traversal defense, and identity."""

from pathlib import Path
import pytest

from chronos.security.identity import create_agent, authenticate_key
from chronos.security.permissions import set_permissions, check_action_allowed
from chronos.security.sanitizer import validate_path_access, redact_secrets
from chronos.security.gates import check_gate_approval

def test_path_traversal_and_symlinks(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    secret_dir = tmp_path / "secrets"
    secret_dir.mkdir()
    (secret_dir / "keys.env").write_text("API_KEY=123")

    # Allowed access
    ok, _ = validate_path_access("src/main.py", tmp_path, allowed_globs=["src/**"])
    assert ok is True

    # Path traversal attack
    bad_traverse, reason1 = validate_path_access("src/../../etc/passwd", tmp_path, allowed_globs=["src/**"])
    assert bad_traverse is False
    assert "escapes repository root" in reason1

    # Restricted file pattern (.env)
    bad_env, reason2 = validate_path_access("secrets/keys.env", tmp_path, allowed_globs=["**"])
    assert bad_env is False
    assert "restricted pattern" in reason2

    # Symlink escape
    symlink_dir = src_dir / "secret_link"
    try:
        symlink_dir.symlink_to(secret_dir, target_is_directory=True)
        bad_symlink, _ = validate_path_access("src/secret_link/keys.env", tmp_path, allowed_globs=["src/**"])
        assert bad_symlink is False
    except OSError:
        pass # Skip symlink test if OS permission denied

def test_agent_identity_and_auth(memory_db):
    agent_info = create_agent(memory_db, name="coder_bot", agent_type="autonomous")
    assert agent_info["agent_id"].startswith("agent_")
    assert agent_info["raw_key"].startswith("chron_")

    # Authenticate valid key
    auth_agent = authenticate_key(memory_db, agent_info["raw_key"])
    assert auth_agent is not None
    assert auth_agent["agent_id"] == agent_info["agent_id"]

    # Reject invalid key
    assert authenticate_key(memory_db, "chron_invalidkey12345") is None

def test_permission_manifest_enforcement(memory_db, tmp_path):
    agent_id = "agent_intern"
    set_permissions(
        memory_db,
        agent_id=agent_id,
        manifest={
            "allowed_paths": ["src/frontend/**"],
            "denied_paths": ["src/frontend/auth/**"],
            "read_only": False,
            "allowed_tools": ["file_editor"],
        },
    )

    # Permitted path
    ok1, _ = check_action_allowed(memory_db, agent_id, "file_editor", "src/frontend/components.tsx", tmp_path)
    assert ok1 is True

    # Denied subpath
    ok2, reason2 = check_action_allowed(memory_db, agent_id, "file_editor", "src/frontend/auth/login.tsx", tmp_path)
    assert ok2 is False
    assert "restricted pattern" in reason2 or "denied" in reason2.lower()

    # Unauthorized tool
    ok3, reason3 = check_action_allowed(memory_db, agent_id, "terminal", "src/frontend/index.ts", tmp_path)
    assert ok3 is False
    assert "allowed_tools" in reason3

def test_secret_scrubbing():
    raw_text = "Connected with API key chron_abcdef1234567890123456 and token sk-live9876543210987654321"
    scrubbed = redact_secrets(raw_text)
    assert "chron_[REDACTED]" in scrubbed
    assert "sk-[REDACTED]" in scrubbed
    assert "chron_abcdef" not in scrubbed
