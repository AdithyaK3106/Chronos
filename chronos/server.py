"""The Chronos MCP server — one server, all four capability layers.

Rationale: a design partner should add ONE block to their agent config, not
four. Before unification each wedge shipped its own FastMCP server, which meant
four commands, four env blocks, and four things to notice were missing when a
tool did not appear. The wedges were never independent products -- they share a
data store and feed each other -- so shipping them as four servers exposed our
internal decomposition as the user's integration problem.

Implementation note: the per-wedge modules remain the source of truth for their
tools; this module re-registers those same functions on a single FastMCP
instance. `@mcp.tool()` returns the undecorated function, so registering it on a
second server is a no-op for the first -- there is no wrapper to unwrap and no
behaviour change. The wedge modules stay independently importable and testable.

Naming: tools keep the names they already had. Wedge 1's tools are
`as_of_callers`/`as_of_callees`/`as_of_impact`/`what_changed`/`index_health`
(not the `chronos_*` prefix the other wedges use). Renaming them would break
every existing agent config for a cosmetic gain, so they are left alone.
"""

import asyncio
import functools
import itertools
import json
import os
import sys
import threading
import time

# Patch sys.stdout.flush to ignore OSError [Errno 22] on Windows
_original_flush = sys.stdout.flush
def _safe_flush():
    try:
        _original_flush()
    except OSError as e:
        if e.errno == 22:
            pass
        else:
            raise
sys.stdout.flush = _safe_flush

from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from . import audit, identity, permissions, sensitive
from .wedge1_mcp import (as_of_callees, as_of_callers, as_of_diff,
                         as_of_impact, index_health, what_changed)
from .wedge2_mcp import (chronos_capture_lesson, chronos_playbook_health,
                         chronos_propose_rule, chronos_query_playbook)
from .wedge3_mcp import (chronos_acquire_lock, chronos_check_conflicts,
                         chronos_log_provenance, chronos_release_lock,
                         chronos_who_touched)
from .wedge4_mcp import (chronos_enforce, chronos_generate_rule,
                         chronos_list_rules, chronos_promote_rule,
                         chronos_rule_report)

mcp = FastMCP("chronos")

# Long enough that a client's opening burst of tool calls has claimed the graph
# before the drain thread tries. Traces are not time-critical; tool latency is.
DRAIN_DELAY = float(os.environ.get("CHRONOS_DRAIN_DELAY", "60"))

# Wedge 1 — temporal graph: what the codebase looked like, whenever you ask.
TOOLS = [as_of_callers, as_of_callees, as_of_impact, as_of_diff, what_changed, index_health,
         # Wedge 3 — intent ledger: what agents are about to change.
         chronos_acquire_lock, chronos_release_lock, chronos_check_conflicts,
         chronos_log_provenance, chronos_who_touched,
         # Wedge 2 — policy playbook: what the standards are, kept current.
         chronos_capture_lesson, chronos_query_playbook, chronos_propose_rule,
         chronos_playbook_health,
         # Wedge 4 — CI enforcement: what does not get to merge.
         chronos_generate_rule, chronos_enforce, chronos_promote_rule,
         chronos_list_rules, chronos_rule_report]
# chronos_sensitive_reads, chronos_check_gate_status, chronos_anomaly_report,
# chronos_trigger_anomaly_check, chronos_trigger_baseline_recompute are added
# to TOOLS below, after they're defined -- they need _authenticate/_check_
# permissions/_tag_sensitive from this same module, so they can't be imported
# from elsewhere the way the wedge tools are. Do not add a bare @mcp.tool()
# for any of them; add the function to TOOLS instead so _track() wraps it.

# In-flight call tracker, for chronos_mcp_status. All tools serialize behind
# the single Kuzu driver's asyncio.Lock (wedge1_mcp.driver()), so when several
# agents share this one server, a slow call (an LLM completion, typically)
# makes every other call wait with no visibility into who's holding things up
# or for how long -- indistinguishable from a graph lock or a dead server.
# This does not change queueing behaviour; it only makes the wait legible.
_IN_FLIGHT = {}
_call_ids = itertools.count()
_tracker_lock = threading.Lock()


def _agent_id_of(kwargs) -> str:
    """Best-effort caller identity. Most tools take agent_id directly;
    chronos_capture_lesson nests it in its trace dict instead."""
    direct = kwargs.get("agent_id")
    if direct:
        return direct
    trace = kwargs.get("trace")
    if isinstance(trace, dict) and trace.get("agent_id"):
        return trace["agent_id"]
    return "(unnamed caller)"


def _node_id_of(kwargs) -> str | None:
    return kwargs.get("node_id")


def _current_api_key() -> str | None:
    """The MCP client's API key, from its process environment.

    ponytail: FastMCP's stdio transport inherits the parent process env, so
    the key set in the client's MCP config env block is just os.environ here.
    """
    return os.environ.get("CHRONOS_API_KEY")


def _authenticate(tool_name: str, kwargs: dict) -> dict:
    """Runs before every tool call. Returns {"ok": True, "agent_id": ...} or
    {"ok": False, "error": {...}}."""
    strict = os.environ.get("CHRONOS_AUTH") == "strict"
    key = _current_api_key()
    result = identity.resolve_key(key)
    if result["status"] == "ok":
        return {"ok": True, "agent_id": result["agent_id"]}
    if not strict:
        # Not enforcing: identity comes from the payload as before, whether
        # or not a key was presented.
        return {"ok": True, "agent_id": _agent_id_of(kwargs)}
    if result["status"] == "no_key":
        return {"ok": False, "error": {"error": "auth_required",
                "message": "CHRONOS_AUTH=strict requires a CHRONOS_API_KEY"}}
    return {"ok": False, "error": {"error": "auth_failed", "reason": result["status"]}}


def _check_permissions(agent_id: str, tool_name: str, kwargs: dict) -> dict:
    node_id = _node_id_of(kwargs)
    check = permissions.check(agent_id, tool_name, node_id)
    if not check["allowed"]:
        try:
            from . import ledger
            ledger.log_event(permissions.db.get_db(), node_id or tool_name, agent_id,
                             kwargs.get("session_id", ""), "permission_denied", check["reason"])
        except Exception:  # noqa: BLE001 -- denial logging must never block the denial
            pass
    return check


def _tag_sensitive(agent_id: str, kwargs: dict) -> None:
    """Side-effect only: logs a sensitive_read provenance+audit entry. Never
    touches file content -- only the path, label, severity, and matched glob."""
    node_id = _node_id_of(kwargs)
    if not node_id:
        return
    hit = sensitive.classify_path(node_id.split("::", 1)[0])
    if not hit:
        return
    try:
        from . import ledger
        # ledger.log_event already writes the provenance row AND appends to
        # the audit log; data_sensitivity/label are added on top of that
        # generic audit entry here so audit consumers can filter by severity
        # without re-parsing the reason JSON.
        ledger.log_event(permissions.db.get_db(), node_id, agent_id,
                         kwargs.get("session_id", ""), "sensitive_read", json.dumps(hit))
    except Exception:  # noqa: BLE001 -- tagging is a side effect, never fatal
        return
    if hit["severity"] == "high" and os.environ.get("CHRONOS_SLACK_WEBHOOK"):
        _notify_slack(f"High-severity sensitive read: {agent_id} read {node_id} "
                      f"({hit['label']})")


def _notify_slack(text: str) -> None:
    webhook = os.environ.get("CHRONOS_SLACK_WEBHOOK")
    if not webhook:
        return
    try:
        import urllib.request
        req = urllib.request.Request(
            webhook, data=json.dumps({"text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
    except Exception:  # noqa: BLE001 -- a notification failure must never break a tool call
        pass


def _track(tool):
    is_async = asyncio.iscoroutinefunction(tool)

    @functools.wraps(tool)
    async def wrapped(*args, **kwargs):
        call_id = next(_call_ids)
        auth = _authenticate(tool.__name__, kwargs)
        if not auth["ok"]:
            return auth["error"]
        agent_id = auth["agent_id"]
        perm = _check_permissions(agent_id, tool.__name__, kwargs)
        if not perm["allowed"]:
            return {"error": "permission_denied", "reason": perm["reason"],
                    "tool": tool.__name__, "agent_id": agent_id}

        entry = {
            "tool": tool.__name__,
            "agent_id": agent_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with _tracker_lock:
            _IN_FLIGHT[call_id] = entry
        try:
            result = await tool(*args, **kwargs) if is_async else tool(*args, **kwargs)
            _tag_sensitive(agent_id, kwargs)
            return result
        finally:
            with _tracker_lock:
                _IN_FLIGHT.pop(call_id, None)
    return wrapped


def chronos_sensitive_reads(agent_id: str = None, since_hours: int = 168) -> dict:
    """Recent reads/writes against paths classified as sensitive (F7).

    Defaults to the last 7 days. Never includes file content -- only path,
    label, severity, and the matched pattern (carried in `reason`)."""
    from . import db as _db
    con = _db.get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).isoformat()
    q = ("SELECT node_id, agent_id, session_id, reason, timestamp FROM provenance_events "
         "WHERE action='sensitive_read' AND timestamp >= ?")
    args = [since]
    if agent_id:
        q += " AND agent_id=?"
        args.append(agent_id)
    rows = con.execute(q + " ORDER BY id DESC LIMIT 200", args).fetchall()
    reads = []
    high = 0
    for r in rows:
        hit = json.loads(r["reason"]) if r["reason"] else {}
        if hit.get("severity") == "high":
            high += 1
        reads.append({"node_id": r["node_id"], "agent_id": r["agent_id"],
                      "session_id": r["session_id"], "label": hit.get("label"),
                      "severity": hit.get("severity"), "timestamp": r["timestamp"]})
    return {"reads": reads, "total": len(reads), "high_severity_count": high}


def chronos_check_gate_status(gate_id: str) -> dict:
    """Poll a sensitive-module gate request (F5): pending, approved, denied, or expired."""
    from . import gates
    gate = gates.get_gate(gate_id)
    if gate is None:
        return {"error": "no such gate", "gate_id": gate_id}
    out = {"gate_id": gate_id, "status": gate["status"], "module": gate["protected_module_label"],
           "requested_at": gate["requested_at"], "resolved_at": gate["resolved_at"],
           "resolved_by": gate["resolved_by"]}
    # No GITHUB_TOKEN means the poll thread never started (server.main()) and
    # this gate will never resolve on its own -- a human has to run the CLI.
    # Make that actionable instead of a silent "pending" forever.
    if gate["status"] == "pending" and not os.environ.get("GITHUB_TOKEN"):
        out["manual_approval_required"] = True
        out["cli_command"] = f"chronos gates approve {gate_id}"
    return out


def chronos_anomaly_report(agent_id: str = None, since_hours: int = 24) -> dict:
    """Recent behavioural anomalies (F6): unusually high volume, off-hours
    activity, path deviation, or a write-lock spike vs. an agent's baseline."""
    from . import anomaly
    return anomaly.report(agent_id, since_hours)


def chronos_trigger_anomaly_check(agent_id: str) -> dict:
    """Test infrastructure: run the anomaly check on an agent's most recent
    session right now, instead of waiting for it to go idle for 30 minutes
    and the sweeper to notice. Not part of the F6 spec's steady state."""
    from . import anomaly, db as _db
    con = _db.get_db()
    rows = con.execute(
        "SELECT node_id, agent_id, session_id, action, timestamp FROM provenance_events "
        "WHERE agent_id=? ORDER BY timestamp", (agent_id,)).fetchall()
    if not rows:
        return {"checked": False, "reason": "no history"}
    sessions = anomaly._sessions_for(rows)
    result = anomaly.check_session(agent_id, sessions[-1], con)
    return {"checked": True, "anomaly": result}


def chronos_trigger_baseline_recompute() -> dict:
    """Test infrastructure: force an immediate baseline recompute instead of
    waiting for the daily sweeper cycle. Not part of the F6 spec's steady
    state -- exists so the stress test can seed history and get a baseline
    without sleeping 24h."""
    from . import anomaly
    n = anomaly.compute_baselines()
    return {"agents_updated": n}


TOOLS += [chronos_sensitive_reads, chronos_check_gate_status, chronos_anomaly_report,
          chronos_trigger_anomaly_check, chronos_trigger_baseline_recompute]
for _tool in TOOLS:
    mcp.tool()(_track(_tool))


# ponytail: chronos_mcp_status is deliberately left outside TOOLS/_track() --
# it has no write path and reveals no secrets (just tool names + durations of
# in-flight calls), and it must stay reachable with no key even when a hung
# call is why a real key can't get a response. Guarding it would make the one
# tool meant to diagnose a stuck server also block on that same stuck server's
# auth path.
@mcp.tool()
def chronos_mcp_status() -> dict:
    """What's currently running on this server, and who's waiting behind it.

    Every tool call serializes behind one process-wide graph driver (see
    wedge1_mcp.driver()), so a slow call -- almost always an LLM completion --
    makes every other agent's call wait with no error and no progress signal.
    Call this before a potentially slow tool, or when a call seems stuck, to
    see whether something else is already running and who owns it, rather
    than assuming the server is dead or the graph is locked."""
    with _tracker_lock:
        calls = sorted(_IN_FLIGHT.values(), key=lambda e: e["started_at"])
    if not calls:
        return {"busy": False, "in_flight": []}
    now = datetime.now(timezone.utc)
    for c in calls:
        c["running_for_seconds"] = round(
            (now - datetime.fromisoformat(c["started_at"])).total_seconds(), 1)
    return {"busy": True, "in_flight": calls}


SWEEP_INTERVAL = float(os.environ.get("CHRONOS_SWEEP_INTERVAL", "60"))


def _sweep_once():
    """One pass: expire locks/gates, rotate the audit log, recompute stale
    anomaly baselines. Never lets one failing step block the others."""
    from . import db as _db, ledger
    con = _db.get_db()
    try:
        n = ledger.sweep_expired(con)
        if n:
            ledger.log_event(con, "*", "chronos-system", "", "lock_expired", f"{n} lock(s) swept")
    except Exception as e:  # noqa: BLE001 -- the sweeper must never crash the server
        print(f"chronos sweeper: lock sweep failed: {e}")
    try:
        audit.maybe_rotate()
    except Exception as e:  # noqa: BLE001
        print(f"chronos sweeper: audit rotation failed: {e}")
    try:
        from . import gates
        gates.expire_stale()
    except Exception as e:  # noqa: BLE001
        print(f"chronos sweeper: gate expiry failed: {e}")
    try:
        from . import anomaly
        anomaly.maybe_recompute_baselines()
        anomaly.check_idle_sessions()
    except Exception as e:  # noqa: BLE001
        print(f"chronos sweeper: anomaly pass failed: {e}")


def _sweep_loop():
    while True:
        time.sleep(SWEEP_INTERVAL)
        _sweep_once()


def _gate_poll_loop():
    from . import gates
    while True:
        time.sleep(60)
        try:
            gates.poll_github_approvals()
        except Exception as e:  # noqa: BLE001 -- polling must never crash the server
            print(f"chronos gate poll: {e}")


def _graph_warmup():
    """Pre-claims the Kuzu driver in the background so the first real graph
    tool call doesn't pay the cold-open cost (up to CHRONOS_DRIVER_TIMEOUT,
    nondeterministically -- see wedge1_mcp.driver()'s docstring). If open
    hangs, it hangs in this thread instead of on a client's call; the
    transport comes up regardless (this thread never touches it) and tool
    calls queue normally once the driver resolves.

    ponytail: calls store.open_driver() directly and writes wedge1_mcp's
    module globals instead of awaiting wedge1_mcp.driver() -- that coroutine
    takes an asyncio.Lock which is not safe to await from two different event
    loops (this thread's vs. mcp.run()'s), and this thread starts before
    mcp.run() so there's no concurrent access to race against yet. If a
    client call still beats this thread to it, driver()'s own asyncio.Lock
    handles that -- this is strictly an optimization, not the only path.
    """
    try:
        from . import wedge1_mcp
        from .store import ensure_schema, open_driver
        wedge1_mcp._driver = open_driver()
        asyncio.run(ensure_schema(wedge1_mcp._driver))
        wedge1_mcp._schema_ready = True
    except Exception as e:  # noqa: BLE001 -- warmup must never crash the server
        print(f"chronos graph warmup: {type(e).__name__}: {e}")


def main():
    threading.Thread(target=_sweep_loop, daemon=True, name="chronos-lock-sweeper").start()
    threading.Thread(target=_graph_warmup, daemon=True, name="chronos-graph-warmup").start()
    if os.environ.get("GITHUB_TOKEN"):
        threading.Thread(target=_gate_poll_loop, daemon=True, name="chronos-gate-poll").start()

    # Drain any test-failure traces captured since the last run. Backgrounded:
    # the server must start whether or not there are traces, and dispatch may
    # make LLM calls (trace_processor swallows its own failures).
    #
    # This thread grounds traces by opening the Kuzu graph, and Kuzu allows one
    # holder per process. Racing the first tool call for it means one of the two
    # waits -- and open_driver() waits rather than failing, so whichever lost
    # simply stopped responding. Deferred until after the transport is up, so
    # the first client request wins the graph and this runs in the quiet after.
    # trace_processor also guards its own open and reflects ungrounded rather
    # than blocking. Opt out entirely with CHRONOS_CAPTURE=0.
    if os.environ.get("CHRONOS_CAPTURE", "1") != "0":
        def _drain_later():
            time.sleep(DRAIN_DELAY)
            try:
                from .trace_processor import process_pending
                process_pending()
            except Exception:  # noqa: BLE001 -- capture must never break the server
                pass
        threading.Thread(target=_drain_later, daemon=True,
                         name="chronos-trace-drain").start()
    mcp.run()


if __name__ == "__main__":
    main()
