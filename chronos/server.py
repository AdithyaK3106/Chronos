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

import os
import threading
import time

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

for _tool in TOOLS:
    mcp.tool()(_tool)


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
