"""Sensitive module gates: human-in-the-loop lock approval (F5).

.chronos/protected.yml lists path globs that require a human to approve a
lock before it's granted. Re-read on mtime change so an operator's edits
take effect without a server restart. GitHub PR-comment approval is optional
(requires GITHUB_TOKEN + GITHUB_REPO); CLI approval always works.
"""

import json
import os
import secrets
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import audit, db

_cache = {"mtime": None, "modules": [], "path": None}


def _config_path() -> Path:
    root = os.environ.get("CHRONOS_REPO_PATH") or "."
    return Path(root).resolve() / ".chronos" / "protected.yml"


def _load() -> list[dict]:
    p = _config_path()
    if not p.is_file():
        return []
    mtime = p.stat().st_mtime
    if _cache["path"] == p and _cache["mtime"] == mtime:
        return _cache["modules"]
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        modules = doc.get("protected_modules") or []
    except (OSError, yaml.YAMLError):
        modules = []
    _cache.update(mtime=mtime, modules=modules, path=p)
    return modules


def is_protected(path: str) -> dict | None:
    import fnmatch
    if not path:
        return None
    norm = path.replace("\\", "/")
    for mod in _load():
        for pat in mod.get("paths", []):
            if fnmatch.fnmatch(norm, pat):
                return mod
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.isoformat()


def request_gate(node_id: str, agent_id: str, session_id: str, module: dict, con=None) -> dict:
    c = con or db.get_db()
    gate_id = secrets.token_hex(12)
    timeout_h = module.get("approval_timeout_hours", 24)
    now = _now()
    expires = now + timedelta(hours=timeout_h)
    c.execute("""
        INSERT INTO gate_requests
            (gate_id, node_id, agent_id, session_id, protected_module_label,
             status, requested_at, expires_at)
        VALUES (?,?,?,?,?,'pending',?,?)
    """, (gate_id, node_id, agent_id, session_id, module["label"],
          _iso(now), _iso(expires)))
    audit.append({"event_type": "gate_requested", "gate_id": gate_id, "node_id": node_id,
                  "agent_id": agent_id, "module": module["label"]})
    pr_number = None
    if module.get("approval_channel") == "pr_comment" and os.environ.get("GITHUB_TOKEN"):
        pr_number = _post_pr_comment(gate_id, node_id, agent_id, module)
        if pr_number:
            c.execute("UPDATE gate_requests SET pr_number=? WHERE gate_id=?", (pr_number, gate_id))
    return {"gate_id": gate_id, "module": module["label"], "pr_number": pr_number,
            "expires_at": _iso(expires)}


def get_gate(gate_id: str, con=None) -> dict | None:
    c = con or db.get_db()
    r = c.execute("SELECT * FROM gate_requests WHERE gate_id=?", (gate_id,)).fetchone()
    return dict(r) if r else None


def list_pending(con=None) -> list[dict]:
    c = con or db.get_db()
    return [dict(r) for r in c.execute(
        "SELECT * FROM gate_requests WHERE status='pending' ORDER BY requested_at").fetchall()]


def _resolve(con, gate_id: str, status: str, resolved_by: str, denial_reason: str = None) -> dict:
    gate = get_gate(gate_id, con)
    if gate is None:
        return {"resolved": False, "reason": "no such gate"}
    if gate["status"] != "pending":
        return {"resolved": False, "reason": f"already {gate['status']}"}
    ts = _iso(_now())
    con.execute(
        "UPDATE gate_requests SET status=?, resolved_at=?, resolved_by=?, denial_reason=? "
        "WHERE gate_id=?", (status, ts, resolved_by, denial_reason, gate_id))
    if status == "approved":
        con.execute("UPDATE intent_locks SET status='held' WHERE node_id=?", (gate["node_id"],))
    else:
        con.execute("DELETE FROM intent_locks WHERE node_id=? AND status='pending_approval'",
                   (gate["node_id"],))
    audit.append({"event_type": f"gate_{status}", "gate_id": gate_id, "node_id": gate["node_id"],
                  "resolved_by": resolved_by, "denial_reason": denial_reason})
    return {"resolved": True, "gate_id": gate_id, "status": status}


def approve(gate_id: str, resolved_by: str, con=None) -> dict:
    return _resolve(con or db.get_db(), gate_id, "approved", resolved_by)


def deny(gate_id: str, resolved_by: str, reason: str = None, con=None) -> dict:
    return _resolve(con or db.get_db(), gate_id, "denied", resolved_by, reason)


def expire_stale(con=None) -> int:
    c = con or db.get_db()
    now = _iso(_now())
    rows = c.execute(
        "SELECT gate_id, node_id FROM gate_requests WHERE status='pending' AND expires_at<=?",
        (now,)).fetchall()
    for r in rows:
        c.execute("UPDATE gate_requests SET status='expired', resolved_at=? WHERE gate_id=?",
                 (now, r["gate_id"]))
        c.execute("DELETE FROM intent_locks WHERE node_id=? AND status='pending_approval'",
                 (r["node_id"],))
        audit.append({"event_type": "gate_expired", "gate_id": r["gate_id"], "node_id": r["node_id"]})
    return len(rows)


def _github_api(path: str, method: str = "GET", body: dict = None):
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPO")
    if not token or not repo:
        return None
    url = f"https://api.github.com/repos/{repo}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError):
        return None


def _most_recent_open_pr() -> int | None:
    prs = _github_api("/pulls?state=open&sort=updated&direction=desc")
    if not prs:
        return None
    return prs[0]["number"]


def _post_pr_comment(gate_id: str, node_id: str, agent_id: str, module: dict) -> int | None:
    pr = _most_recent_open_pr()
    if pr is None:
        return None
    body = (
        f"**Chronos gate request** -- human approval required\n\n"
        f"- Agent: `{agent_id}`\n"
        f"- Requesting lock on: `{node_id}`\n"
        f"- Protected module: **{module['label']}**\n"
        f"- Requested: {_iso(_now())}\n"
        f"- Gate ID: `{gate_id}`\n\n"
        f"Reply with `/chronos approve {gate_id}` to approve or "
        f"`/chronos deny {gate_id} <reason>` to deny."
    )
    _github_api(f"/issues/{pr}/comments", method="POST", body={"body": body})
    return pr


def poll_github_approvals(con=None) -> int:
    """Scan the most recent open PR's comments for /chronos approve|deny.
    Returns how many gates were resolved. No-op if GITHUB_TOKEN unset."""
    if not os.environ.get("GITHUB_TOKEN"):
        return 0
    c = con or db.get_db()
    pending = {g["gate_id"]: g for g in list_pending(c) if g["pr_number"]}
    if not pending:
        return 0
    resolved = 0
    for pr_number in {g["pr_number"] for g in pending.values()}:
        comments = _github_api(f"/issues/{pr_number}/comments") or []
        for comment in comments:
            text = (comment.get("body") or "").strip()
            author = (comment.get("user") or {}).get("login", "unknown")
            for gate_id, gate in list(pending.items()):
                if gate["pr_number"] != pr_number:
                    continue
                if f"/chronos approve {gate_id}" in text:
                    approve(gate_id, author, c)
                    resolved += 1
                    pending.pop(gate_id, None)
                elif f"/chronos deny {gate_id}" in text:
                    reason = text.split(f"/chronos deny {gate_id}", 1)[1].strip() or None
                    deny(gate_id, author, reason, c)
                    resolved += 1
                    pending.pop(gate_id, None)
    return resolved
