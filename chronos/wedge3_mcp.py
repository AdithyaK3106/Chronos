"""MCP surface for the intent/provenance ledger (Wedge 3).

Agent-agnostic: agent_id and session_id are whatever string the caller passes.
There is no auth layer yet, so these identify but do not authenticate -- a lock
prevents accidental collision between cooperating agents, not a hostile one.
"""

from mcp.server.fastmcp import FastMCP

from . import ledger

mcp = FastMCP("chronos-ledger")

_con = None


def con():
    global _con
    if _con is None:
        _con = ledger.connect()
    return _con


@mcp.tool()
def chronos_acquire_lock(node_id: str, agent_id: str, session_id: str = "",
                         intent: str = "", ttl_seconds: int = ledger.DEFAULT_TTL) -> dict:
    """Declare intent to modify a node, blocking other agents from it.

    node_id is the qualified name from the structural graph (e.g.
    "src/api.ts::createClient::Function"). Returns acquired=False plus the current
    holder's agent_id and intent if someone else holds it. Expired locks are swept
    first, so a crashed agent never wedges a node. Re-acquiring your own lock
    extends it.
    """
    return ledger.acquire(con(), node_id, agent_id, session_id, intent, ttl_seconds)


@mcp.tool()
def chronos_release_lock(node_id: str, agent_id: str, session_id: str = "") -> dict:
    """Release an intent lock. Only the agent that acquired it may release it.

    On a successful release, adds a suggestion (not an instruction) to consider
    chronos_propose_rule if the work just finished taught something durable.
    Locking is the closest signal Wedge 3 has to "this was deliberate, scoped
    work" -- most tool calls are too frequent/low-signal to nudge on, a
    completed lock is not. The LLM decides; nothing is proposed automatically.
    """
    result = ledger.release(con(), node_id, agent_id, session_id)
    if result.get("released"):
        intent = result.get("intent") or "this change"
        result["suggestion"] = (
            f"Lock released on {node_id} (intent: \"{intent}\"). If this work "
            f"surfaced a durable lesson -- a footgun, a convention worth "
            f"enforcing, a pattern other agents should follow -- consider "
            f"calling chronos_propose_rule. Skip it if nothing here "
            f"generalizes; most changes don't need a rule."
        )
    return result


@mcp.tool()
def chronos_check_conflicts(node_ids: list[str]) -> dict:
    """Check a set of nodes for active locks before starting multi-node work.

    Returns which are locked (with holder and intent) and which are free, so an
    agent can plan around a conflict instead of discovering it halfway through.
    """
    return ledger.check_conflicts(con(), node_ids)


@mcp.tool()
def chronos_log_provenance(node_id: str, agent_id: str, session_id: str = "",
                           action: str = "modified", reason: str = "") -> dict:
    """Record that an agent touched a node, and why. Append-only; always succeeds."""
    return ledger.log_event(con(), node_id, agent_id, session_id, action, reason)


@mcp.tool()
def chronos_who_touched(node_id: str, limit: int = 20) -> dict:
    """Provenance history for a node: who changed it, when, and why. Newest first."""
    return ledger.history(con(), node_id, limit)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
