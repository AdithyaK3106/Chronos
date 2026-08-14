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

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import db

HTML = Path(__file__).parent / "dashboard.html"

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
    rules = rows("SELECT count(*) n FROM enforcement_rules")
    viol = rows(f"SELECT count(*) n FROM provenance_events "
                f"WHERE action IN ({_in(BLOCK_ACTIONS)}) "
                f"AND date(timestamp) = date('now')", BLOCK_ACTIONS)
    return {
        "total_nodes": graph_node_count(),
        "active_locks": locks[0]["n"] if locks else 0,
        "rules_total": rules[0]["n"] if rules else 0,
        "violations_today": viol[0]["n"] if viol else 0,
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
    for r in rows("SELECT rule_id, language, rule_text, status, created_at, "
                  "promoted_at, detectability_passed, false_positive_risk "
                  "FROM enforcement_rules"):
        # [S3] no rule_id column on events; the enforcer writes "rule <id>: ..."
        hits = rows(f"SELECT count(*) n FROM provenance_events "
                    f"WHERE action IN ({_in(BLOCK_ACTIONS + WARN_ACTIONS)}) "
                    f"AND reason LIKE ?",
                    BLOCK_ACTIONS + WARN_ACTIONS + (f"rule {r['rule_id']}:%",))
        out.append({
            "rule_id": r["rule_id"],
            "name": r["rule_text"] or r["rule_id"],   # [S5]
            "language": r["language"],
            "status": r["status"],
            "fired_count": hits[0]["n"] if hits else 0,
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


def serve(host="127.0.0.1", port=8080):
    import uvicorn
    print(f"Chronos dashboard running at http://{host}:{port}")
    print("Open in your browser - auto-refreshes every 30s")
    uvicorn.run(app, host=host, port=port, log_level="warning")
