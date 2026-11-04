"""Data for the dashboard Overview: "what did Chronos do for me?"

Every number here is computed from local state (audit log, Chronos DB, Claude Code settings,
benchmark DB). When a source is missing the section says so and how to fill it; nothing is
estimated or filled in with a default.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from chronos.integrations.claude_code import HOOK_ENVELOPE_MODE, _agent_id
_GATE_LEGACY_MARKER = "requires human gate approval"
_HOOK_STATUS_TTL = 60.0
_hook_status_cache: dict[str, tuple[float, dict[str, Any]]] = {}


# ----------------------------------------------------------------------------- decisions
def classify_decision(record: dict[str, Any], protected_mode: str) -> str:
    """allowed | asked | denied | error, as the developer experienced it in Claude Code.

    The firewall records a protected-module hold as status=deny/category=gate; with the default
    policy (protected_modules: ask) Claude Code shows that as a question, not a refusal.
    """
    status = str(record.get("status", "")).lower()
    if status == "allow":
        return "allowed"
    if status == "error":
        return "error"
    if status == "ask":
        return "asked"
    if status in ("violation", "log"):
        return "flagged"
    category = (record.get("metadata") or {}).get("category", "")
    if category == "error":
        return "error"
    is_gate = category == "gate" or (not category and _GATE_LEGACY_MARKER in str(record.get("reason", "")))
    if is_gate and protected_mode == "ask":
        return "asked"
    return "denied"


def _read_audit(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.is_file():
        return []
    out = []
    with open(log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_type") == "tool_authorization":
                out.append(rec)
    return out


def _parse_ts(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def decisions_section(root: Path, protected_mode: str, now: datetime, latest: int = 8) -> dict[str, Any]:
    from chronos.core.paths import audit_log_path

    records = _read_audit(audit_log_path(root))
    day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    empty = {"allowed": 0, "asked": 0, "denied": 0, "error": 0, "flagged": 0}
    today, week = dict(empty), dict(empty)
    rows = []
    for rec in records:
        kind = classify_decision(rec, protected_mode)
        ts = _parse_ts(rec.get("timestamp", ""))
        if ts and ts >= week_start:
            week[kind] += 1
            if ts >= day_start:
                today[kind] += 1
        rows.append({
            "timestamp": rec.get("timestamp"),
            "decision": kind,
            "target": rec.get("target"),
            "reason": rec.get("reason"),
            "agent_id": rec.get("agent_id"),
        })
    return {
        "available": bool(records),
        "today": today,
        "week": week,
        "total_recorded": len(records),
        # Interesting first: holds and denials are what the developer wants to review.
        "latest": list(reversed(rows))[:latest],
        "latest_not_allowed": [r for r in reversed(rows) if r["decision"] != "allowed"][:latest],
        "empty_hint": None if records else (
            "No tool decisions recorded yet. They appear once Claude Code runs with the Chronos hooks "
            "(`chronos claude install`) in this repository."
        ),
    }


# ----------------------------------------------------------------------------- health
def _hook_status(root: Path, probe: bool) -> dict[str, Any]:
    from chronos.integrations import claude_code as cc

    key = f"{root}|{probe}"
    hit = _hook_status_cache.get(key)
    if hit and time.monotonic() - hit[0] < _HOOK_STATUS_TTL:
        return hit[1]
    scopes = {}
    for scope in ("project", "user"):
        try:
            st = cc.status(scope, root) if probe else _status_without_probe(cc, scope, root)
            scopes[scope] = {
                "settings": st["settings"],
                "hooks_installed": st["hooks_installed"],
                "hook_commands_work": st["hook_commands_work"],
                "fails_closed": st.get("fails_closed", False),
            }
        except Exception as exc:  # noqa: BLE001 - unreadable settings must not break the page
            scopes[scope] = {"error": f"{type(exc).__name__}: {exc}"}
    installed = sorted({h for s in scopes.values() for h in s.get("hooks_installed", [])})
    works = [s.get("hook_commands_work") for s in scopes.values() if s.get("hooks_installed")]
    fails_closed = any(s.get("fails_closed") for s in scopes.values() if "PreToolUse" in s.get("hooks_installed", []))
    errors = cc.recent_hook_errors()
    result = {
        "installed": installed,
        "complete": {"PreToolUse", "PostToolUse", "UserPromptSubmit"} <= set(installed),
        "fails_closed": fails_closed,
        "recent_errors": len(errors),
        "last_error": errors[-1] if errors else None,
        "commands_work": (all(works) if works and None not in works else None),
        "scopes": scopes,
        "fix": (
            "Run `chronos claude install` (all your projects) or `chronos claude install --scope project` (this one)."
            if not installed else
            "Re-run `chronos claude install` to add the Bash checks (PostToolUse)." if "PostToolUse" not in installed else
            "Re-run `chronos claude install` (Claude Code 2.1.295+) so a crashed or timed-out check blocks the action." if not fails_closed else None
        ),
    }
    _hook_status_cache[key] = (time.monotonic(), result)
    return result


def clear_hook_status_cache() -> None:
    _hook_status_cache.clear()


def _status_without_probe(cc: Any, scope: str, root: Path) -> dict[str, Any]:
    path = cc.settings_path(scope, root)
    installed = []
    if path.exists():
        hooks = json.loads(path.read_text(encoding="utf-8")).get("hooks", {})
        installed = [n for n, es in hooks.items() if any(cc.is_chronos_hook(h.get("command", "")) for e in es for h in e.get("hooks", []))]
    return {"settings": str(path), "hooks_installed": installed, "hook_commands_work": None,
            "fails_closed": cc.pre_hook_fails_closed(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else False}


def _sandbox_status() -> dict[str, Any]:
    from chronos.sandbox.runner import SandboxUnavailable, _require_bwrap

    try:
        _require_bwrap()
        return {"available": True, "backend": "bubblewrap", "fix": None}
    except SandboxUnavailable as exc:
        return {"available": False, "backend": None, "fix": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "backend": None, "fix": f"Sandbox probe failed: {exc}"}


def _index_status(root: Path, con: sqlite3.Connection | None, now: datetime) -> dict[str, Any]:
    import os

    from chronos.context.indexer import SUPPORTED_EXTENSIONS, prune_ignored_dirs

    nodes = con.execute("SELECT count(*) FROM code_nodes").fetchone()[0] if con else 0
    marker = root / ".chronos" / "last_index"
    last = datetime.fromtimestamp(marker.stat().st_mtime, tz=timezone.utc) if marker.exists() else None
    changed = None
    if last is not None:
        cutoff = marker.stat().st_mtime
        changed = 0
        for dirpath, dirnames, filenames in os.walk(root):
            prune_ignored_dirs(dirpath, dirnames)
            for fname in filenames:
                if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                    try:
                        changed += os.stat(os.path.join(dirpath, fname)).st_mtime > cutoff
                    except OSError:
                        pass
    fix = None if nodes else "Run `chronos sync` here, or send a prompt in Claude Code: the hook indexes on first use."
    return {
        "indexed": nodes > 0,
        "symbols": nodes,
        "last_indexed_at": last.isoformat() if last else None,
        "age_seconds": int((now - last).total_seconds()) if last else None,
        # Source files modified since the last index; the next prompt hook re-indexes them.
        "changed_files": changed,
        "note": None if last or not nodes else "Indexed outside the Claude Code hook, so the index time is unknown.",
        "fix": fix,
    }


def _audit_status(root: Path) -> dict[str, Any]:
    from chronos.audit.logger import verify_audit_chain

    from chronos.core.paths import audit_log_path

    log = audit_log_path(root)
    if not log.is_file():
        return {"valid": None, "records": 0, "message": "No audit log yet.", "fix": None}
    ok, msg, count = verify_audit_chain(log)
    return {
        "valid": ok,
        "records": count,
        "message": msg,
        "fix": None if ok else "The audit chain does not verify. Run `chronos audit verify` for the first bad line.",
    }


# ----------------------------------------------------------------------------- protection
def protection_section(root: Path, con: sqlite3.Connection | None, protected_mode: str) -> dict[str, Any]:
    from chronos.security.gates import load_protected_modules

    try:
        modules = load_protected_modules(root)
    except Exception:  # noqa: BLE001
        modules = []
    patterns = []
    for mod in modules:
        # Default modules carry one `pattern`; custom ones from .chronos/protected.yml carry `paths`.
        pats = [mod["pattern"]] if mod.get("pattern") else list(mod.get("paths") or [])
        for pat in pats:
            patterns.append({"module": mod.get("name", pat), "pattern": pat})
    grants = []
    if con is not None:
        try:
            for row in con.execute(
                "SELECT agent_id, path_pattern, approved_by, created_at, expires_at FROM gate_approvals ORDER BY created_at DESC"
            ):
                grants.append(dict(row))
        except sqlite3.Error:
            pass
    return {
        "mode": protected_mode,
        "protected": patterns,
        "grants": grants,
        "grant_command": f"chronos gate grant '<file-or-pattern>' --agent {_hook_agent_id()}",
        "explain": (
            "Edits to protected files pause and ask you first (policy: protected_modules = ask)."
            if protected_mode == "ask" else
            "Edits to protected files are refused unless granted (policy: protected_modules = deny)."
        ),
    }


def _hook_agent_id() -> str:
    return _agent_id({})


# ----------------------------------------------------------------------------- context
def context_section(con: sqlite3.Connection | None, repo_id: str, now: datetime, latest: int = 6) -> dict[str, Any]:
    if con is None:
        return {"available": False, "latest": [], "week": {"injections": 0, "tokens": 0, "memories": 0}}
    week_start = (now - timedelta(days=7)).isoformat()
    rows = con.execute(
        """SELECT envelope_id, prompt, actual_tokens, resolved_files, memory_entries, created_at
           FROM context_envelopes WHERE repo_id = ? AND mode = ? ORDER BY created_at DESC""",
        (repo_id, HOOK_ENVELOPE_MODE),
    ).fetchall()
    week = {"injections": 0, "tokens": 0, "memories": 0}
    items = []
    for r in rows:
        files = json.loads(r["resolved_files"] or "[]")
        mems = json.loads(r["memory_entries"] or "[]")
        if r["created_at"] >= week_start:
            week["injections"] += 1
            week["tokens"] += int(r["actual_tokens"] or 0)
            week["memories"] += len(mems)
        if len(items) < latest:
            items.append({
                "envelope_id": r["envelope_id"],
                "created_at": r["created_at"],
                "prompt": (r["prompt"] or "")[:140],
                "tokens": int(r["actual_tokens"] or 0),
                "files": files[:5],
                "file_count": len(files),
                "memories": mems,
            })
    return {
        "available": bool(rows),
        "total": len(rows),
        "week": week,
        "latest": items,
        "empty_hint": None if rows else (
            "No context injected yet. When a prompt in Claude Code matches code in this repository, the "
            "hook adds the relevant files and memories and it is listed here."
        ),
    }


# ----------------------------------------------------------------------------- efficiency
def efficiency_section(root: Path) -> dict[str, Any]:
    """Paired benchmark deltas, straight from benchmarks/data/benchmark_runs.db, with n and models."""
    db = root / "benchmarks" / "data" / "benchmark_runs.db"
    if not db.is_file():
        return {
            "available": False,
            "empty_hint": (
                "Not measured for this repository, so no number is shown. Chronos's paired benchmark (the same "
                "tasks with and without Chronos) runs from a Chronos source checkout: `python -m benchmarks.cli run`."
            ),
        }
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        pairs = con.execute(
            """SELECT b.task_id, b.total_tokens bt, t.total_tokens tt, b.duration_seconds bs, t.duration_seconds ts,
                      b.status bstat, t.status tstat, b.resolved_model_id model
               FROM runs b JOIN runs t ON b.task_id = t.task_id AND b.iteration = t.iteration
               WHERE b.arm = 'baseline' AND t.arm = 'treatment'"""
        ).fetchall()
    finally:
        con.close()
    pairs = [p for p in pairs if (p["bt"] or 0) > 0 and (p["tt"] or 0) > 0]
    if not pairs:
        return {"available": False, "empty_hint": "The benchmark database has no complete baseline/Chronos pairs yet."}
    tot_b = sum(p["bt"] for p in pairs)
    tot_t = sum(p["tt"] for p in pairs)
    per_task = [1 - p["tt"] / p["bt"] for p in pairs]
    time_deltas = [p["ts"] - p["bs"] for p in pairs if p["bs"] is not None and p["ts"] is not None]
    models = sorted({p["model"] for p in pairs if p["model"]})
    ok = lambda s: str(s).lower() in ("success", "passed", "resolved")  # noqa: E731
    return {
        "available": True,
        "pairs": len(pairs),
        "tasks": len({p["task_id"] for p in pairs}),
        "models": models,
        "token_reduction_pct": round(100 * (1 - tot_t / tot_b), 1),
        "median_task_token_reduction_pct": round(100 * statistics.median(per_task), 1),
        "median_time_delta_s": round(statistics.median(time_deltas), 1) if time_deltas else None,
        "success": {
            "baseline": sum(ok(p["bstat"]) for p in pairs),
            "chronos": sum(ok(p["tstat"]) for p in pairs),
        },
        "caveat": (
            f"Measured on {len(pairs)} paired runs ({len({p['task_id'] for p in pairs})} tasks) with "
            f"{', '.join(models) or 'unrecorded models'}. Results from this benchmark suite, not a guarantee "
            "for your sessions."
        ),
        "source": str(db.relative_to(root)),
    }


# ----------------------------------------------------------------------------- assembly
def build_overview(identity: Any, probe_hooks: bool = True, now: datetime | None = None) -> dict[str, Any]:
    from chronos.core.config import ChronosConfig, PolicyConfig
    from chronos.core.db import get_connection

    now = now or datetime.now(timezone.utc)
    root = Path(identity.root)
    try:
        policy = ChronosConfig.load(root).policy
    except Exception:  # noqa: BLE001
        policy = PolicyConfig()
    protected_mode = getattr(policy, "protected_modules", "ask")

    db_path = Path(identity.db_path)
    con = get_connection(db_path) if db_path.is_file() else None
    try:
        return {
            "repo": {"id": identity.id, "name": identity.name, "root": str(root)},
            "generated_at": now.isoformat(),
            "health": {
                "hooks": _hook_status(root, probe_hooks),
                "sandbox": _sandbox_status(),
                "index": _index_status(root, con, now),
                "audit": _audit_status(root),
            },
            "decisions": decisions_section(root, protected_mode, now),
            "protection": protection_section(root, con, protected_mode),
            "context": context_section(con, identity.id, now),
            "efficiency": efficiency_section(root),
        }
    finally:
        if con is not None:
            con.close()
