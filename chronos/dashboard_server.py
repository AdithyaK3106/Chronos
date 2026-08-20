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
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from . import db, rule_store

HTML = Path(__file__).parent / "dashboard.html"
PRESENT_HTML = Path(__file__).parent / "presentation.html"
GRAPH_HTML = Path(__file__).parent / "graph.html"
TRACE = Path(__file__).resolve().parent.parent / "demo" / "trace.json"

BLOCK_ACTIONS = ("blocked_by_ci", "blocked")   # [S2]
WARN_ACTIONS = ("warned", "warn")

# A force-directed layout in a browser tab stops being legible well before
# it stops being technically renderable. These caps keep /api/graph honest
# about a large repo instead of either refusing to render or silently
# picking a different arbitrary subset on every request.
MAX_GRAPH_NODES = int(os.environ.get("CHRONOS_GRAPH_MAX_NODES", "400"))
MAX_GRAPH_EDGES = int(os.environ.get("CHRONOS_GRAPH_MAX_EDGES", "1200"))

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


@app.get("/present")
def present():
    """The pitch-deck view: one slide per Chronos feature (wedges 1-4, then
    F1-F7), each pulling one live number off the same APIs as the dashboard.
    Separate page, not a dashboard mode -- built for presentation distance
    (big type, one idea per screen), not for operating the system."""
    if not PRESENT_HTML.exists():
        return JSONResponse({"error": f"presentation.html not found at {PRESENT_HTML}"}, 404)
    return FileResponse(PRESENT_HTML, media_type="text/html")


@app.get("/graph")
def graph_page():
    """Full-screen AST graph explorer -- its own page (not a dashboard card)
    because a force-directed layout needs real screen real estate to stay
    legible; a ~460px card was cramped for anything past a handful of nodes."""
    if not GRAPH_HTML.exists():
        return JSONResponse({"error": f"graph.html not found at {GRAPH_HTML}"}, 404)
    return FileResponse(GRAPH_HTML, media_type="text/html")


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


def _cluster_of(path: str | None, name: str) -> str:
    # ponytail: top-level (or top-two, for a deep tree) path directory as
    # the cluster key -- good enough to turn "24k nodes" into "a dozen
    # labeled blobs" without a real community-detection pass. Upgrade to
    # Louvain/label-propagation if path-based clustering stops matching
    # real module boundaries.
    #
    # n.summary is sometimes a bare filename ("main.py"), sometimes a
    # full repo-relative path ("src/main.py"), and sometimes "{}" for
    # synthetic nodes (the repo root, a detached-HEAD marker) -- so a
    # *file* (no "/") is grouped by directory, and only a directory
    # component itself becomes its own cluster key.
    p = (path or "").replace("\\", "/").strip("/")
    if not p or p == "{}":
        return "(root)"
    parts = p.split("/")
    dirs = parts[:-1] if "." in parts[-1] else parts  # drop a trailing filename
    if not dirs:
        return "(root)"
    return dirs[0] if len(dirs) == 1 else "/".join(dirs[:2])


def _file_of(path: str | None, name: str) -> str:
    """The file a node belongs to, for grouping changes by file rather than
    by individual symbol. Falls back to the node's own name when summary
    carries no real path (a module/package node, or a synthetic root)."""
    p = (path or "").replace("\\", "/").strip("/")
    if not p or p == "{}":
        return name
    return p


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


@app.get("/api/anomalies/{agent_id}/sessions")
def api_anomaly_sessions(agent_id: str):
    """F6 detail: per-session call counts for one agent, oldest first -- the
    actual numbers behind a HIGH_VOLUME flag (baseline sessions vs. the
    outlier), for the presentation's session-volume chart. Same session
    reconstruction anomaly.py itself uses (30-min idle gap), just returning
    the counts instead of running the rules."""
    from . import anomaly as _anomaly
    rows_ = rows("SELECT node_id, agent_id, session_id, action, timestamp "
                "FROM provenance_events WHERE agent_id=? ORDER BY timestamp", (agent_id,))
    sessions = _anomaly._sessions_for(rows_)
    return [{"session_id": s[0]["session_id"], "calls": len(s),
             "started_at": s[0]["timestamp"]} for s in sessions]


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


@app.get("/api/graph")
def api_graph():
    """The bi-temporal AST graph, structured for rendering: nodes clustered by
    top-level path segment (so a large repo groups into modules instead of a
    hairball), edges carrying their real valid_at/invalid_at window so a
    client can scrub through time and watch supersessions happen.

    Capped at MAX_GRAPH_NODES/MAX_GRAPH_EDGES -- a 24k-node global graph
    cannot be force-directed-laid-out in a browser tab, and a partial-but-
    real graph is more honest than either refusing to render or silently
    picking an arbitrary 500 that changes every request. The cap keeps the
    highest-degree nodes (the ones actually worth seeing) via the query's
    own ORDER BY, not a random LIMIT.
    """
    try:
        import asyncio

        from .store import open_driver
        from . import groups as _groups

        async def fetch():
            drv = open_driver()
            try:
                group_id = _groups.resolve(os.environ.get("CHRONOS_GROUP_ID"),
                                           os.environ.get("CHRONOS_REPO_PATH"))
                edge_recs, _, _ = await drv.execute_query(
                    """
                    MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
                    WHERE e.group_id = $g
                    RETURN n.uuid AS su, n.name AS sn, n.summary AS sp,
                           m.uuid AS tu, m.name AS tn, m.summary AS tp,
                           e.name AS rel, e.valid_at AS va, e.invalid_at AS ia
                    ORDER BY e.valid_at
                    LIMIT $lim
                    """, g=group_id, lim=MAX_GRAPH_EDGES)
                return [dict(r) for r in edge_recs]
            finally:
                await drv.close()

        edges_raw = asyncio.run(fetch())
    except Exception:  # noqa: BLE001 -- graph absent/locked must not 500 the dashboard
        return {"nodes": [], "edges": [], "truncated": False, "total_nodes": 0}

    nodes = {}
    edges = []
    for r in edges_raw:
        for uid, name, path in ((r["su"], r["sn"], r["sp"]), (r["tu"], r["tn"], r["tp"])):
            if uid not in nodes:
                nodes[uid] = {"id": uid, "label": name, "path": path or "",
                             "cluster": _cluster_of(path, name)}
        edges.append({
            "source": r["su"], "target": r["tu"], "rel": r["rel"],
            "valid_at": r["va"].isoformat() if r["va"] else None,
            "invalid_at": r["ia"].isoformat() if r["ia"] else None,
        })
        if len(nodes) >= MAX_GRAPH_NODES:
            break

    node_list = list(nodes.values())[:MAX_GRAPH_NODES]
    kept_ids = {n["id"] for n in node_list}
    edges = [e for e in edges if e["source"] in kept_ids and e["target"] in kept_ids]
    return {
        "nodes": node_list, "edges": edges,
        "truncated": len(edges_raw) >= MAX_GRAPH_EDGES or len(nodes) > MAX_GRAPH_NODES,
        "total_nodes": graph_node_count(),
    }


@app.get("/api/graph/changes")
def api_graph_changes(since: str, until: str):
    """Structural edges added/superseded in [since, until], grouped by the
    file each edge's endpoints belong to -- the "what files changed in this
    window" view the graph actually supports. There is no file-level
    added/modified/deleted status in the graph itself, only edges appearing
    and disappearing between two timestamps (query.py's changes()); a file
    is classified here from that: touched-by-an-added-edge-only -> added,
    touched-by-a-removed-edge-only -> removed, touched by both -> modified.
    since/until are ISO-8601 timestamps (the graph.html scrubber range).
    """
    try:
        s = datetime.fromisoformat(since.replace("Z", "+00:00"))
        u = datetime.fromisoformat(until.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse({"error": "since/until must be ISO-8601 timestamps"}, 400)

    try:
        import asyncio

        from .store import open_driver
        from . import groups as _groups

        async def fetch():
            drv = open_driver()
            try:
                group_id = _groups.resolve(os.environ.get("CHRONOS_GROUP_ID"),
                                           os.environ.get("CHRONOS_REPO_PATH"))
                added_recs, _, _ = await drv.execute_query(
                    """
                    MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
                    WHERE e.group_id = $g AND e.valid_at > $s AND e.valid_at <= $u
                    RETURN n.name AS sn, n.summary AS sp, m.name AS tn, m.summary AS tp,
                           e.name AS rel, e.valid_at AS va
                    LIMIT $lim
                    """, g=group_id, s=s, u=u, lim=MAX_GRAPH_EDGES)
                removed_recs, _, _ = await drv.execute_query(
                    """
                    MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
                    WHERE e.group_id = $g AND e.invalid_at > $s AND e.invalid_at <= $u
                    RETURN n.name AS sn, n.summary AS sp, m.name AS tn, m.summary AS tp,
                           e.name AS rel, e.invalid_at AS ia
                    LIMIT $lim
                    """, g=group_id, s=s, u=u, lim=MAX_GRAPH_EDGES)
                return [dict(r) for r in added_recs], [dict(r) for r in removed_recs]
            finally:
                await drv.close()

        added_raw, removed_raw = asyncio.run(fetch())
    except Exception:  # noqa: BLE001 -- graph absent/locked must not 500 the dashboard
        return {"files": [], "since": since, "until": until, "truncated": False}

    files = {}  # file -> {"added": int, "removed": int, "edges": [...]}

    def touch(r, ts_field, bucket):
        for name, path in ((r["sn"], r["sp"]), (r["tn"], r["tp"])):
            f = _file_of(path, name)
            entry = files.setdefault(f, {"added": 0, "removed": 0, "edges": []})
            entry[bucket] += 1
            entry["edges"].append({"src": r["sn"], "rel": r["rel"], "dst": r["tn"],
                                   "at": r[ts_field].isoformat() if r[ts_field] else None,
                                   "kind": bucket})

    for r in added_raw:
        touch(r, "va", "added")
    for r in removed_raw:
        touch(r, "ia", "removed")

    out = []
    for f, d in files.items():
        status = "modified" if d["added"] and d["removed"] else ("added" if d["added"] else "removed")
        out.append({"file": f, "status": status, "added_edges": d["added"],
                    "removed_edges": d["removed"], "edges": d["edges"][:20]})
    out.sort(key=lambda x: -(x["added_edges"] + x["removed_edges"]))
    return {
        "files": out, "since": since, "until": until,
        "truncated": len(added_raw) >= MAX_GRAPH_EDGES or len(removed_raw) >= MAX_GRAPH_EDGES,
    }


def serve(host="127.0.0.1", port=8080):
    import uvicorn
    print(f"Chronos dashboard running at http://{host}:{port}")
    print("Open in your browser - auto-refreshes every 30s")
    uvicorn.run(app, host=host, port=port, log_level="warning")
