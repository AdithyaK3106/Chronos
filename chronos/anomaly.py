"""Agent behavioural anomaly detection (F6).

Sessions are reconstructed from provenance_events (consecutive rows for the
same agent_id, split on a 30-minute idle gap). Agents need 7+ days of
history before a baseline exists; before that they're in "learning mode"
and never flagged.
"""

import json
import os
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import audit, db

IDLE_GAP = timedelta(minutes=30)
MIN_DAYS_FOR_BASELINE = 7
MIN_DAYS_FOR_OFF_HOURS = 14


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _sessions_for(rows: list) -> list[list]:
    """Split timestamp-ordered provenance rows for one agent into sessions."""
    sessions, current = [], []
    prev_ts = None
    for r in rows:
        ts = _parse(r["timestamp"])
        if prev_ts is not None and ts - prev_ts > IDLE_GAP:
            sessions.append(current)
            current = []
        current.append(r)
        prev_ts = ts
    if current:
        sessions.append(current)
    return sessions


def _session_metrics(session: list) -> dict:
    prefixes = [r["node_id"].split("::", 1)[0].split("/")[0] for r in session if r["node_id"]]
    start = _parse(session[0]["timestamp"])
    end = _parse(session[-1]["timestamp"])
    return {
        "tool_calls": len(session),
        "unique_prefixes": len(set(prefixes)),
        "duration_seconds": (end - start).total_seconds(),
        "write_locks": sum(1 for r in session if r["action"] in ("lock_acquired",)),
        "start_hour": start.hour,
        "prefixes": prefixes,
    }


def _all_agent_ids(con) -> list[str]:
    return [r["agent_id"] for r in con.execute(
        "SELECT DISTINCT agent_id FROM provenance_events").fetchall()]


def compute_baselines(con=None) -> int:
    """Recompute agent_baselines for every agent with >=7 days of history.
    Returns how many agents got a fresh baseline."""
    c = con or db.get_db()
    updated = 0
    for agent_id in _all_agent_ids(c):
        rows = c.execute(
            "SELECT node_id, action, timestamp FROM provenance_events "
            "WHERE agent_id=? ORDER BY timestamp", (agent_id,)).fetchall()
        if not rows:
            continue
        first, last = _parse(rows[0]["timestamp"]), _parse(rows[-1]["timestamp"])
        days = (last - first).total_seconds() / 86400
        if days < MIN_DAYS_FOR_BASELINE:
            continue
        sessions = _sessions_for(rows)
        metrics = [_session_metrics(s) for s in sessions]
        hour_buckets = [0] * 24
        prefix_counter = Counter()
        for m in metrics:
            hour_buckets[m["start_hour"]] += 1
            prefix_counter.update(m["prefixes"])
        n = len(metrics)
        c.execute("""
            INSERT INTO agent_baselines
                (agent_id, computed_at, days_of_history, avg_tool_calls_per_session,
                 avg_unique_files_per_session, avg_session_duration_seconds,
                 active_hour_buckets, avg_write_locks_per_session, common_path_prefixes)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(agent_id) DO UPDATE SET
                computed_at=excluded.computed_at, days_of_history=excluded.days_of_history,
                avg_tool_calls_per_session=excluded.avg_tool_calls_per_session,
                avg_unique_files_per_session=excluded.avg_unique_files_per_session,
                avg_session_duration_seconds=excluded.avg_session_duration_seconds,
                active_hour_buckets=excluded.active_hour_buckets,
                avg_write_locks_per_session=excluded.avg_write_locks_per_session,
                common_path_prefixes=excluded.common_path_prefixes
        """, (agent_id, _now().isoformat(), round(days),
              sum(m["tool_calls"] for m in metrics) / n,
              sum(m["unique_prefixes"] for m in metrics) / n,
              sum(m["duration_seconds"] for m in metrics) / n,
              json.dumps(hour_buckets),
              sum(m["write_locks"] for m in metrics) / n,
              json.dumps([p for p, _ in prefix_counter.most_common(5)])))
        updated += 1
    return updated


def maybe_recompute_baselines(con=None) -> int:
    """Recompute once per 24h, tracked by the newest computed_at in the table."""
    c = con or db.get_db()
    row = c.execute("SELECT MAX(computed_at) m FROM agent_baselines").fetchone()
    if row and row["m"] and _now() - _parse(row["m"]) < timedelta(hours=24):
        return 0
    return compute_baselines(c)


def get_baseline(agent_id: str, con=None) -> dict | None:
    c = con or db.get_db()
    r = c.execute("SELECT * FROM agent_baselines WHERE agent_id=?", (agent_id,)).fetchone()
    if r is None:
        return None
    return {
        "agent_id": r["agent_id"], "computed_at": r["computed_at"],
        "days_of_history": r["days_of_history"],
        "avg_tool_calls_per_session": r["avg_tool_calls_per_session"],
        "avg_unique_files_per_session": r["avg_unique_files_per_session"],
        "avg_session_duration_seconds": r["avg_session_duration_seconds"],
        "active_hour_buckets": json.loads(r["active_hour_buckets"]),
        "avg_write_locks_per_session": r["avg_write_locks_per_session"],
        "common_path_prefixes": json.loads(r["common_path_prefixes"]),
    }


def check_session(agent_id: str, session_rows: list, con=None) -> dict | None:
    """Apply the anomaly rules to one already-ended session. Returns the
    anomaly_events row dict if any rule fired, else None (including
    learning-mode agents, which are never flagged)."""
    c = con or db.get_db()
    baseline = get_baseline(agent_id, c)
    if baseline is None or not session_rows:
        return None
    m = _session_metrics(session_rows)
    types = []

    if m["tool_calls"] > baseline["avg_tool_calls_per_session"] * 3:
        types.append("HIGH_VOLUME")

    if (baseline["days_of_history"] >= MIN_DAYS_FOR_OFF_HOURS
            and baseline["active_hour_buckets"][m["start_hour"]] == 0):
        types.append("OFF_HOURS")

    common = set(baseline["common_path_prefixes"])
    if m["prefixes"]:
        outside = sum(1 for p in m["prefixes"] if p not in common)
        if outside / len(m["prefixes"]) > 0.30:
            types.append("PATH_DEVIATION")

    if m["write_locks"] > baseline["avg_write_locks_per_session"] * 5:
        types.append("HIGH_WRITE_VOLUME")

    if not types:
        return None

    severity = "low" if len(types) == 1 else ("medium" if len(types) == 2 else "high")
    session_id = session_rows[0]["session_id"] if "session_id" in session_rows[0].keys() else ""
    detected_at = _now().isoformat()
    session_metrics = {k: v for k, v in m.items() if k != "prefixes"}
    baseline_metrics = {k: v for k, v in baseline.items()
                        if k not in ("agent_id", "computed_at", "active_hour_buckets",
                                     "common_path_prefixes")}
    c.execute("""
        INSERT INTO anomaly_events
            (agent_id, session_id, detected_at, anomaly_types, session_metrics,
             baseline_metrics, severity)
        VALUES (?,?,?,?,?,?,?)
    """, (agent_id, session_id, detected_at, json.dumps(types),
          json.dumps(session_metrics), json.dumps(baseline_metrics), severity))
    audit.append({"event_type": "anomaly", "agent_id": agent_id, "anomaly_types": types,
                  "severity": severity, "timestamp": detected_at})
    if os.environ.get("CHRONOS_SLACK_WEBHOOK"):
        _notify_slack(f"Anomaly detected for {agent_id}: {', '.join(types)} (severity: {severity})")
    return {"agent_id": agent_id, "session_id": session_id, "detected_at": detected_at,
            "anomaly_types": types, "severity": severity}


def _notify_slack(text: str) -> None:
    webhook = os.environ.get("CHRONOS_SLACK_WEBHOOK")
    if not webhook:
        return
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except (urllib.error.URLError, urllib.error.HTTPError):
        pass


def check_idle_sessions(con=None) -> int:
    """Called from the sweeper: find agents whose most recent event is past
    the idle gap (their session just 'ended') and run the anomaly check on
    that session if it hasn't been checked yet."""
    c = con or db.get_db()
    checked = 0
    cutoff = _now() - IDLE_GAP
    for agent_id in _all_agent_ids(c):
        rows = c.execute(
            "SELECT node_id, agent_id, session_id, action, timestamp FROM provenance_events "
            "WHERE agent_id=? ORDER BY timestamp", (agent_id,)).fetchall()
        if not rows:
            continue
        sessions = _sessions_for(rows)
        last = sessions[-1]
        if _parse(last[-1]["timestamp"]) > cutoff:
            continue  # still active
        already = c.execute(
            "SELECT 1 FROM anomaly_events WHERE agent_id=? AND session_id=?",
            (agent_id, last[0]["session_id"])).fetchone()
        if already:
            continue  # this idle session already flagged an anomaly; don't re-flag it
        # ponytail: a clean (non-anomalous) session leaves no anomaly_events row, so
        # it gets rechecked every sweep until the agent's next session starts. Cheap
        # (one SELECT + a metrics pass) and correct; add a "sessions_checked" table
        # only if this shows up in profiling.
        check_session(agent_id, last, c)
        checked += 1
    return checked


def report(agent_id: str = None, since_hours: int = 24, con=None) -> dict:
    c = con or db.get_db()
    since = (_now() - timedelta(hours=since_hours)).isoformat()
    q = "SELECT * FROM anomaly_events WHERE detected_at >= ?"
    args = [since]
    if agent_id:
        q += " AND agent_id=?"
        args.append(agent_id)
    rows = c.execute(q + " ORDER BY detected_at DESC", args).fetchall()
    anomalies = [{"agent_id": r["agent_id"], "session_id": r["session_id"],
                 "detected_at": r["detected_at"], "anomaly_types": json.loads(r["anomaly_types"]),
                 "severity": r["severity"]} for r in rows]
    all_agents = _all_agent_ids(c)
    learning = [a for a in all_agents if get_baseline(a, c) is None]
    return {"anomalies": anomalies, "learning_mode_agents": learning, "total_agents": len(all_agents)}
