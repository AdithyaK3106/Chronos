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

from mcp.server.fastmcp import FastMCP

from .wedge1_mcp import (as_of_callees, as_of_callers, as_of_impact,
                         index_health, what_changed)
from .wedge2_mcp import (chronos_capture_lesson, chronos_playbook_health,
                         chronos_propose_rule, chronos_query_playbook)
from .wedge3_mcp import (chronos_acquire_lock, chronos_check_conflicts,
                         chronos_log_provenance, chronos_release_lock,
                         chronos_who_touched)
from .wedge4_mcp import (chronos_enforce, chronos_generate_rule,
                         chronos_list_rules, chronos_promote_rule,
                         chronos_rule_report)

mcp = FastMCP("chronos")

# Wedge 1 — temporal graph: what the codebase looked like, whenever you ask.
TOOLS = [as_of_callers, as_of_callees, as_of_impact, what_changed, index_health,
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
    mcp.run()


if __name__ == "__main__":
    main()
