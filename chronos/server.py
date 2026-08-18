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
import os
import threading
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

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


def _track(tool):
    @functools.wraps(tool)
    async def wrapped(*args, **kwargs):
        call_id = next(_call_ids)
        entry = {
            "tool": tool.__name__,
            "agent_id": _agent_id_of(kwargs),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        with _tracker_lock:
            _IN_FLIGHT[call_id] = entry
        try:
            return await tool(*args, **kwargs)
        finally:
            with _tracker_lock:
                _IN_FLIGHT.pop(call_id, None)
    return wrapped


for _tool in TOOLS:
    mcp.tool()(_track(_tool) if asyncio.iscoroutinefunction(_tool) else _tool)


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


def main():
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
