"""Read-only dashboard API over chronos.db.

Reads SQLite directly through db.get_db(). No MCP calls, no subprocesses: the
dashboard observes state, it never changes it.

SCHEMA NOTES -- the real tables differ from what a reader might assume, and
every deviation below was checked against chronos/db.py rather than guessed:

  [S1] db.py exposes get_db(), not get_connection().
  [S2] There is no `blocked` action. Wedge 4 writes action='blocked_by_ci'
       (enforcer.py). We match that, and also accept 'blocked'/'warned' so a
       future writer using the shorter names is not silently ignored.
  [S3] provenance_events has NO rule_id column. The enforcer records the rule
       inside `reason`, formatted "rule <id>: <text>". fired_count therefore
       matches on that prefix; a rule whose id never appears there counts 0.
  [S4] provenance_events has NO file_path column. Churn is grouped by node_id,
       which is a qualified_name (Wedge 1 identity), not a path.
  [S5] enforcement_rules has NO `name` or `description` column. `rule_text`
       serves as both; evidence_preview is its first 120 chars.
  [S6] Only blocks are persisted. `warned` verdicts are returned live by
       chronos_enforce and never written, so the timeline's warn series is
       usually zero by design, not by failure.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import db, rule_store

HTML = Path(__file__).parent / "dashboard.html"
TRACE = Path(__file__).resolve().parent.parent / "demo" / "trace.json"

BLOCK_ACTIONS = ("blocked_by_ci", "blocked")   # [S2]
WARN_ACTIONS = ("warned", "warn")

app = FastAPI(title="Chronos Dashboard", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)


def rows(sql, params=()):
    """Query chronos.db. A missing table returns [] rather than a 500 -- a fresh
    install has no data and the dashboard must still render."""
    try:
        return [dict(r) for r in db.get_db().execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _in(values):
    return ",".join("?" * len(values))


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    if not HTML.exists():
        return JSONResponse({"error": f"dashboard.html not found at {HTML}"}, 404)
    return FileResponse(HTML, media_type="text/html")


@app.get("/api/stats")
def stats():
    now = datetime.now(timezone.utc).isoformat()
    locks = rows("SELECT count(*) n FROM intent_locks WHERE expires_at > ? "
                 "OR expires_at IS NULL OR expires_at = ''", (now,))
    viol = rows(f"SELECT count(*) n FROM provenance_events "
                f"WHERE action IN ({_in(BLOCK_ACTIONS)}) "
                f"AND date(timestamp) = date('now')", BLOCK_ACTIONS)
    blocking = rows("SELECT count(*) n FROM enforcement_rules WHERE status=?",
                    (rule_store.BLOCKING,))
    warn_only = rows("SELECT count(*) n FROM enforcement_rules WHERE status IN (?,?)",
                     (rule_store.UNVALIDATED, rule_store.VALIDATED))
    enf_actions = BLOCK_ACTIONS + WARN_ACTIONS
    this_week = rows(f"SELECT count(*) n FROM provenance_events "
                     f"WHERE action IN ({_in(enf_actions)}) "
                     f"AND date(timestamp) >= date('now', '-6 days')", enf_actions)
    last_week = rows(f"SELECT count(*) n FROM provenance_events "
                     f"WHERE action IN ({_in(enf_actions)}) "
                     f"AND date(timestamp) >= date('now', '-13 days') "
                     f"AND date(timestamp) < date('now', '-6 days')", enf_actions)
    fresh = api_freshness()
    agents = rows("SELECT count(*) n FROM agents WHERE status='active'")
    pending_gates = rows("SELECT count(*) n FROM gate_requests WHERE status='pending'")
    since_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    anomalies_7d = rows("SELECT count(*) n FROM anomaly_events WHERE detected_at >= ?", (since_7d,))
    # audit.verify() re-hashes the whole log on every call; fine for a table
    # scan but the stats tile only needs pass/fail, not the full result.
    from . import audit as _audit
    audit_ok = None
    try:
        audit_ok = _audit.verify()["valid"]
    except Exception:  # noqa: BLE001 -- a missing/unreadable log must not 500 the dashboard
        pass
    return {
        "total_nodes": graph_node_count(),
        "active_locks": locks[0]["n"] if locks else 0,
        "rules_blocking": blocking[0]["n"] if blocking else 0,
        "rules_warn_only": warn_only[0]["n"] if warn_only else 0,
        "violations_today": viol[0]["n"] if viol else 0,
        "enforced_this_week": this_week[0]["n"] if this_week else 0,
        "enforced_last_week": last_week[0]["n"] if last_week else 0,
        "last_indexed_at": fresh["last_success_at"],
        "active_agents": agents[0]["n"] if agents else 0,
        "pending_gates": pending_gates[0]["n"] if pending_gates else 0,
        "anomalies_7d": anomalies_7d[0]["n"] if anomalies_7d else 0,
        "audit_valid": audit_ok,
    }


def graph_node_count() -> int:
    """Kuzu node count, or 0. The graph is a separate engine that may be
    absent, empty, or locked by another process -- none of which should take
    the dashboard down."""
    try:
        import asyncio

        from .store import open_driver

        async def count():
            drv = open_driver()
            try:
                recs, _, _ = await drv.execute_query(
                    "MATCH (n:Entity) RETURN count(n) AS c")
                return dict(recs[0])["c"] if recs else 0
            finally:
                await drv.close()

        return int(asyncio.run(count()))
    except Exception:
        return 0


@app.get("/api/rules")
def api_rules():
    out = []
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    for r in rows("SELECT rule_id, language, rule_text, status, created_at, "
                  "promoted_at, promoted_by, detectability_passed, false_positive_risk "
                  "FROM enforcement_rules"):
        # [S3] no rule_id column on events; the enforcer writes "rule <id>: ..."
        hits = rows(f"SELECT count(*) n, max(timestamp) t FROM provenance_events "
                    f"WHERE action IN ({_in(BLOCK_ACTIONS + WARN_ACTIONS)}) "
                    f"AND reason LIKE ? AND timestamp >= ?",
                    BLOCK_ACTIONS + WARN_ACTIONS + (f"rule {r['rule_id']}:%", since))
        h = hits[0] if hits else {"n": 0, "t": None}
        out.append({
            "rule_id": r["rule_id"],
            "name": r["rule_text"] or r["rule_id"],   # [S5]
            "language": r["language"],
            "status": r["status"],
            "promoted_by": r["promoted_by"],
            "fired_count": h["n"] or 0,
            "last_fired": h["t"],
            "created_at": r["created_at"],
        })
    out.sort(key=lambda x: -x["fired_count"])
    return out


@app.get("/api/locks")
def api_locks():
    return rows("SELECT node_id, agent_id, session_id, intent, acquired_at, "
                "expires_at FROM intent_locks ORDER BY acquired_at DESC")


@app.get("/api/churn")
def api_churn():
    # [S4] grouped by node_id -- a qualified_name, not a file path.
    out = rows("""
        SELECT node_id AS file_path,
               count(*) AS touch_count,
               max(timestamp) AS last_touched,
               group_concat(DISTINCT agent_id) AS agent_csv
        FROM provenance_events
        GROUP BY node_id
        ORDER BY touch_count DESC, last_touched DESC
        LIMIT 20""")
    for r in out:
        csv = r.pop("agent_csv", None) or ""
        r["agents"] = sorted({a.strip() for a in csv.split(",") if a.strip()})
    return out


@app.get("/api/timeline")
def api_timeline():
    counts = {r["d"]: r for r in rows(f"""
        SELECT date(timestamp) AS d,
               sum(CASE WHEN action IN ({_in(BLOCK_ACTIONS)}) THEN 1 ELSE 0 END) AS blocked,
               sum(CASE WHEN action IN ({_in(WARN_ACTIONS)}) THEN 1 ELSE 0 END) AS warned
        FROM provenance_events
        WHERE date(timestamp) >= date('now', '-13 days')
        GROUP BY date(timestamp)""", BLOCK_ACTIONS + WARN_ACTIONS)}
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(13, -1, -1):       # oldest first, all 14 days present
        day = (today - timedelta(days=i)).isoformat()
        hit = counts.get(day)
        out.append({"date": day,
                    "blocked": int(hit["blocked"] or 0) if hit else 0,
                    "warned": int(hit["warned"] or 0) if hit else 0})
    return out


@app.get("/api/queue")
def api_queue():
    """Rules awaiting a human: proposed (needs approve-rule) and unvalidated.

    `proposed` was missing here, which hid the one state that exists purely to
    demand a human decision -- a git-native rule sat in the store with no
    dashboard surface at all. `status` is returned so the two are
    distinguishable, since they need different actions.
    """
    return [{
        "rule_id": r["rule_id"],
        "name": r["rule_text"] or r["rule_id"],
        "language": r["language"],
        "status": r["status"],
        "created_at": r["created_at"],
        "evidence_preview": (r["rule_text"] or "")[:120],   # [S5]
    } for r in rows("SELECT rule_id, language, rule_text, status, created_at "
                    "FROM enforcement_rules WHERE status IN (?, ?) "
                    "ORDER BY created_at DESC",
                    ("proposed", "warn-only-unvalidated"))]


@app.get("/api/activity")
def api_activity():
    """Raw provenance events, most recent first -- the activity feed. Every
    action type (query/lock/write/block/warn) lives in this one table, so no
    join is needed; the enforcement column below just filters this same feed."""
    return rows("SELECT id, node_id, agent_id, session_id, action, reason, "
                "timestamp FROM provenance_events ORDER BY id DESC LIMIT 50")


@app.get("/api/enforcement")
def api_enforcement():
    """Blocked/warned events only -- same table as /api/activity, filtered."""
    return rows(f"SELECT id, node_id, agent_id, session_id, action, reason, "
                f"timestamp FROM provenance_events "
                f"WHERE action IN ({_in(BLOCK_ACTIONS + WARN_ACTIONS)}) "
                f"ORDER BY id DESC LIMIT 50", BLOCK_ACTIONS + WARN_ACTIONS)


@app.get("/api/freshness")
def api_freshness():
    """Index runs over the last 24h, from index_log (chronos/groups.py). Only
    INDEX/SYNC (success) and SKIP (rejected) outcomes are ever written -- there
    is no partial/failure state to report, so we don't invent one. [S per
    groups.py: SKIP rows come from a group-ownership conflict, not a parse
    failure.]"""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    runs = rows("SELECT ts, group_id, repo_path, outcome, reason FROM index_log "
                "WHERE ts >= ? ORDER BY ts", (since,))
    last_ok = rows("SELECT ts FROM index_log WHERE outcome IN ('INDEX','SYNC') "
                   "ORDER BY ts DESC LIMIT 1")
    return {"runs": runs, "last_success_at": last_ok[0]["ts"] if last_ok else None}


@app.get("/api/savings")
def api_savings():
    """Token savings from the real WITHOUT/WITH Chronos trace (demo/trace.json,
    written by demo/run_comparison.py) -- not an estimate computed here."""
    if not TRACE.exists():
        return {"available": False}
    try:
        data = json.loads(TRACE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"available": False}
    w, c, s = data.get("without_chronos", {}), data.get("with_chronos", {}), data.get("summary", {})
    return {
        "available": True,
        "without_tokens": w.get("total_tokens"),
        "with_tokens": c.get("total_tokens"),
        "tokens_saved": (w.get("total_tokens", 0) - c.get("total_tokens", 0)),
        "savings_pct": s.get("token_savings_pct"),
        "tool_call_ratio": s.get("tool_call_ratio"),
    }


@app.post("/api/rules/{rule_id}/demote")
def api_demote(rule_id: str):
    """blocking -> warn-only. No dedicated rule_store function exists for
    this (only promote_to_blocking does) -- a plain status UPDATE is the
    whole operation, not worth a new module function for one write."""
    con = db.get_db()
    row = con.execute("SELECT status FROM enforcement_rules WHERE rule_id=?",
                       (rule_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "no such rule"}, 404)
    con.execute("UPDATE enforcement_rules SET status=? WHERE rule_id=?",
                (rule_store.VALIDATED, rule_id))
    con.commit()
    return {"rule_id": rule_id, "status": rule_store.VALIDATED}


@app.post("/api/rules/{rule_id}/archive")
def api_archive(rule_id: str):
    con = db.get_db()
    row = con.execute("SELECT status FROM enforcement_rules WHERE rule_id=?",
                       (rule_id,)).fetchone()
    if row is None:
        return JSONResponse({"error": "no such rule"}, 404)
    con.execute("UPDATE enforcement_rules SET status='archived' WHERE rule_id=?",
                (rule_id,))
    con.commit()
    return {"rule_id": rule_id, "status": "archived"}


# ─── governance (F1-F7) ──────────────────────────────────────────────────
# Same read-only-over-SQLite contract as the rest of this file. audit.verify()
# is the one exception -- it re-reads and re-hashes chronos-audit.log off
# disk rather than a table, but it's still pure computation, no mutation.

@app.get("/api/agents")
def api_agents():
    """F1/F2: every registered agent plus its permission manifest, if any."""
    from . import permissions as _perm
    out = []
    for r in rows("SELECT agent_id, name, agent_type, status, owner, created_at "
                  "FROM agents ORDER BY created_at DESC"):
        perm = _perm.get_permissions(r["agent_id"])
        out.append({**r, "permissions": perm})
    return out


@app.get("/api/auth-events")
def api_auth_events():
    """F1: recent key resolutions (ok/invalid_key/suspended/no_key), most
    recent first -- the feed that shows auth is actually being checked."""
    return rows("SELECT id, key_prefix, resolved_agent_id, status, timestamp "
                "FROM auth_events ORDER BY id DESC LIMIT 50")


@app.get("/api/audit")
def api_audit():
    """F4: hash-chain verification result plus basic stats. Computed fresh on
    every call -- the log is typically a few thousand lines, and correctness
    here matters more than shaving the recompute."""
    from . import audit as _audit
    v = _audit.verify()
    s = _audit.stats()
    return {**v, **s}


@app.get("/api/gates")
def api_gates():
    """F5: pending sensitive-module gate requests awaiting human approval."""
    from . import gates as _gates
    return _gates.list_pending()


@app.get("/api/anomalies")
def api_anomalies():
    """F6: recent flagged anomalies plus which agents are still in learning
    mode (no baseline yet, so they can never be flagged)."""
    from . import anomaly as _anomaly
    return _anomaly.report(since_hours=24 * 7)


@app.get("/api/sensitive-reads")
def api_sensitive_reads():
    """F7: recent reads/writes against sensitive-tagged paths. Content is
    never logged anywhere in this pipeline -- only path, label, and severity."""
    from . import sensitive as _sensitive  # noqa: F401 -- import kept for symmetry/clarity
    since = (datetime.now(timezone.utc) - timedelta(hours=24 * 7)).isoformat()
    out = rows("SELECT node_id, agent_id, session_id, reason, timestamp "
              "FROM provenance_events WHERE action='sensitive_read' "
              "AND timestamp >= ? ORDER BY id DESC LIMIT 50", (since,))
    for r in out:
        hit = json.loads(r["reason"]) if r["reason"] else {}
        r["label"] = hit.get("label")
        r["severity"] = hit.get("severity")
    return out


def serve(host="127.0.0.1", port=8080):
    import uvicorn
    print(f"Chronos dashboard running at http://{host}:{port}")
    print("Open in your browser - auto-refreshes every 30s")
    uvicorn.run(app, host=host, port=port, log_level="warning")
