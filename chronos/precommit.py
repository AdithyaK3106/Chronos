"""Tool-agnostic lock (F3) and gate (F5) enforcement at commit time.

Runs inside the git pre-commit hook Chronos installs, alongside the existing
`chronos enforce` rule check. Unlike F3/F5 as MCP tools, this fires on every
commit regardless of whether the agent that wrote the diff ever called
Chronos -- it reads chronos.db directly, the same store the MCP server writes.

Never blocks a commit because Chronos itself is unavailable or unconfigured:
a missing/locked chronos.db or missing protected.yml means "nothing to
check", not "reject". Only a real conflict or a real pending/denied gate
blocks.
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import gates
from .db import db_path

_BUSY_TIMEOUT_MS = 2000


def _repo_root(repo: str | None = None) -> Path:
    return Path(repo or os.environ.get("CHRONOS_REPO_PATH") or ".").resolve()


def _staged_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        return []
    return [f for f in out.stdout.split("\n") if f.strip()]


def _committer_identity(repo: Path) -> str:
    out = subprocess.run(["git", "-C", str(repo), "config", "user.email"],
                         capture_output=True, text=True, timeout=10)
    email = out.stdout.strip()
    if email:
        return email.lower()
    out = subprocess.run(["git", "-C", str(repo), "config", "user.name"],
                         capture_output=True, text=True, timeout=10)
    return out.stdout.strip().lower()


def _same_identity(agent_id: str, committer: str) -> bool:
    if not committer:
        return False
    return agent_id.strip().lower() == committer


def _open_readonly(path: Path) -> sqlite3.Connection | None:
    """A short-busy-timeout connection for a read-time check.

    Deliberately not db.connect(): that applies WAL/migrations and a 5s
    timeout meant for a long-lived server. A commit hook needs to fail open
    fast, not wait behind a writer -- 2s, then skip the check."""
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_MS / 1000)
        con.row_factory = sqlite3.Row
        con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # Confirm the tables this module reads actually exist -- a stray/empty
        # file at this path should behave like "no chronos.db", not raise.
        con.execute("SELECT 1 FROM intent_locks LIMIT 1")
        return con
    except sqlite3.Error:
        return None


def _block(reason: str, file: str, detail: str, action: str):
    print("CHRONOS: commit blocked")
    print(f"  reason:   {reason}")
    print(f"  file:     {file}")
    print(f"  detail:   {detail}")
    print(f"  action:   {action}")


def _warn(text: str):
    print(f"CHRONOS WARNING: {text}", file=sys.stderr)


def check_locks(repo: Path, files: list[str], con: sqlite3.Connection) -> bool:
    """Returns True if the commit is clear to proceed."""
    if os.environ.get("CHRONOS_SKIP_LOCK_CHECK"):
        return True

    # A read-only connection can't sweep (DELETE); filter expired locks out of
    # the query instead, which has the same "expired == free" effect without
    # needing a write. The MCP-side sweep (ledger.sweep_expired) still runs on
    # the write path and does the real cleanup.
    import datetime
    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    committer = _committer_identity(repo)
    rows = con.execute("SELECT * FROM intent_locks WHERE expires_at > ?", (now_iso,)).fetchall()
    if not rows:
        return True

    ok = True
    for f in files:
        norm = f.replace("\\", "/")
        for r in rows:
            node_id = r["node_id"]
            if not (node_id == norm or node_id.startswith(norm + ":") or node_id.startswith(norm + "::")):
                continue
            if _same_identity(r["agent_id"], committer):
                continue
            _block(
                "lock_conflict", f,
                f'locked by agent "{r["agent_id"]}" -- intent: "{r["intent"]}" '
                f'-- expires {r["expires_at"]}',
                f'coordinate with "{r["agent_id"]}", wait for the lock to expire, '
                f'or run: chronos locks release {node_id}',
            )
            ok = False
    return ok


def check_gates(repo: Path, files: list[str], con: sqlite3.Connection) -> bool:
    """Returns True if the commit is clear to proceed."""
    if os.environ.get("CHRONOS_SKIP_GATE_CHECK"):
        return True
    if not gates._config_path().is_file():
        return True

    ok = True
    for f in files:
        norm = f.replace("\\", "/")
        module = gates.is_protected(norm)
        if module is None:
            continue

        row = con.execute(
            "SELECT * FROM gate_requests WHERE node_id LIKE ? "
            "ORDER BY requested_at DESC LIMIT 1",
            (norm + "%",),
        ).fetchone()

        if row is None:
            _warn(
                f'{norm} is protected ("{module["label"]}") but no gate request was '
                "found. Consider routing agent edits through Chronos for full "
                "governance coverage."
            )
            continue

        if row["status"] == "pending":
            _block(
                "gate_pending", norm,
                f'gate {row["gate_id"]} for "{row["protected_module_label"]}" is pending '
                f'-- requested by "{row["agent_id"]}" at {row["requested_at"]}',
                f'chronos gates approve {row["gate_id"]}  (or wait for PR-comment approval '
                "if configured)",
            )
            ok = False
        elif row["status"] == "denied":
            _block(
                "gate_denied", norm,
                f'gate {row["gate_id"]} was denied by "{row["resolved_by"]}"'
                + (f': {row["denial_reason"]}' if row["denial_reason"] else ""),
                "request a new gate, or have the denial overridden",
            )
            ok = False
        # approved / expired: allow silently.
    return ok


def run(repo: str | None = None) -> int:
    """Run both checks against the currently staged diff. Returns an exit code."""
    root = _repo_root(repo)
    files = _staged_files(root)
    if not files:
        return 0

    con = _open_readonly(db_path())
    if con is None:
        return 0  # no chronos.db, or it's unreadable/locked -- never block for that

    try:
        locks_ok = check_locks(root, files, con)
        gates_ok = check_gates(root, files, con)
    finally:
        con.close()

    return 0 if (locks_ok and gates_ok) else 1


def status(repo: str | None = None) -> dict:
    root = _repo_root(repo)
    hook_path = root / ".git" / "hooks" / "pre-commit"
    protected = gates._config_path()
    dbp = db_path()
    con = _open_readonly(dbp)
    readable = con is not None
    if con is not None:
        con.close()
    return {
        "hook_installed": hook_path.is_file() and "chronos-managed" in hook_path.read_text(encoding="utf-8")
        if hook_path.is_file() else False,
        "hook_path": str(hook_path),
        "protected_yml": str(protected) if protected.is_file() else None,
        "chronos_db": str(dbp),
        "chronos_db_readable": readable,
    }
