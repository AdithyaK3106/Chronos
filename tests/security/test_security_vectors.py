import os
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

def test_path_traversal_and_symlinks():
    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir).resolve()
        allowed_dir = base / "src"
        allowed_dir.mkdir()
        secret_dir = base / "secrets"
        secret_dir.mkdir()
        secret_file = secret_dir / "api_keys.env"
        secret_file.write_text("SECRET=12345")

        # Create symlink inside allowed_dir pointing to secret_dir
        symlink_path = allowed_dir / "sneaky_link"
        try:
            symlink_path.symlink_to(secret_dir, target_is_directory=True)
        except OSError:
            pass # Windows might require admin for symlinks

        allowed_globs = ["src/**"]
        denied_globs = ["secrets/**", ".*", "**/.env*"]

        def validate_path(target_path_str: str) -> tuple[bool, str]:
            # Canonicalize and resolve all symlinks and parent traversals
            target = (base / target_path_str).resolve()
            
            # Security check 1: Must remain inside repository root
            try:
                rel = target.relative_to(base)
            except ValueError:
                return False, f"Path traversal attack: {target_path_str} escapes repository root"

            rel_str = rel.as_posix()
            
            # Security check 2: Deny list check
            import fnmatch
            for pat in denied_globs:
                if fnmatch.fnmatch(rel_str, pat) or fnmatch.fnmatch(target.name, pat):
                    return False, f"Denied path policy matched: {pat}"

            # Security check 3: Allowed list check
            matched_allowed = any(fnmatch.fnmatch(rel_str, pat) for pat in allowed_globs)
            if not matched_allowed:
                return False, f"Path {rel_str} not permitted by manifest: {allowed_globs}"

            # Security check 4: If target exists, check real path behind symlink
            if target.exists():
                real_target = target.resolve()
                try:
                    real_rel = real_target.relative_to(base).as_posix()
                except ValueError:
                    return False, "Symlink escape: resolved path escapes repo root"
                
                for pat in denied_globs:
                    if fnmatch.fnmatch(real_rel, pat):
                        return False, f"Symlink escape: target resolves to denied pattern {pat}"

            return True, "Allowed"

        # Test cases:
        attacks = [
            ("src/main.py", True),
            ("src/../../secrets/api_keys.env", False),
            ("../../etc/passwd", False),
            ("secrets/api_keys.env", False),
            (".env", False),
            ("src/sneaky_link/api_keys.env", False), # Symlink escape attempt!
        ]
        
        for p, expected in attacks:
            allowed, reason = validate_path(p)
            print(f"Path: {p:35} -> Allowed: {allowed:5} (Expected: {expected:5}) | Reason: {reason}")
            assert allowed == expected, f"Failed on {p}: expected {expected}, got {allowed}"

def test_stale_lock_protection():
    import sqlite3
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE intent_locks (
            node_id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            intent TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    def acquire_lock(node_id, agent_id, session_id, intent, ttl=300):
        now = datetime.now(timezone.utc)
        now_str = now.isoformat()
        expires_str = (now + timedelta(seconds=ttl)).isoformat()
        
        with con:
            # First clean up expired lock on this node if any
            con.execute("DELETE FROM intent_locks WHERE node_id = ? AND expires_at < ?", (node_id, now_str))
            
            # Atomic INSERT or CONFLICT
            try:
                con.execute(
                    "INSERT INTO intent_locks VALUES (?,?,?,?,?,?)",
                    (node_id, agent_id, session_id, intent, now_str, expires_str)
                )
                return {"acquired": True, "reason": None}
            except sqlite3.IntegrityError:
                row = con.execute("SELECT agent_id, intent, expires_at FROM intent_locks WHERE node_id = ?", (node_id,)).fetchone()
                if row[0] == agent_id:
                    # Renew
                    con.execute("UPDATE intent_locks SET expires_at = ? WHERE node_id = ?", (expires_str, node_id))
                    return {"acquired": True, "renewed": True}
                return {"acquired": False, "conflict": {"agent_id": row[0], "intent": row[1], "expires_at": row[2]}}

    # Agent A acquires
    res1 = acquire_lock("fn_1", "agent_a", "s1", "refactor")
    assert res1["acquired"] is True
    # Agent B blocked
    res2 = acquire_lock("fn_1", "agent_b", "s2", "bugfix")
    assert res2["acquired"] is False
    assert res2["conflict"]["agent_id"] == "agent_a"
    # Agent A renews
    res3 = acquire_lock("fn_1", "agent_a", "s1", "refactor", ttl=600)
    assert res3["acquired"] is True and res3.get("renewed") is True
    print("Lock tests passed: Atomic acquire, renewal, and mutual exclusion verified.")

if __name__ == "__main__":
    test_path_traversal_and_symlinks()
    test_stale_lock_protection()
