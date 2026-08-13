"""MCP surface for the self-improving playbook (Wedge 2).

capture_lesson is what an agent calls after being corrected; query_playbook is what
it calls before starting work. Packmind is the store — Chronos only reflects,
curates, and reads back.
"""

import os

from mcp.server.fastmcp import FastMCP

from . import curator, reflector
from .playbook import Packmind, PackmindError
from .store import open_driver

GROUP = os.environ.get("CHRONOS_GROUP_ID", "default")
mcp = FastMCP("chronos-playbook")

_driver = None
_pm = None


async def driver():
    global _driver
    if _driver is None:
        _driver = open_driver()
    return _driver


def pm():
    global _pm
    if _pm is None:
        _pm = Packmind()
    return _pm


@mcp.tool()
async def chronos_capture_lesson(trace: dict) -> dict:
    """Learn a coding standard from a failed agent action.

    trace: {agent_id, session_id, action, outcome, error_or_correction,
            nodes_touched: [qualified_name], timestamp}
    Runs Reflector (grounded in the temporal graph) then Curator, and on success
    creates an unpublished Packmind standard for a human to approve."""
    store = pm()  # raises loudly if Packmind is not configured/reachable
    candidate = await reflector.reflect(await driver(), GROUP, trace)
    if candidate is None:
        return {
            "candidate_rule": None,
            "submitted": False,
            "packmind_proposal_id": None,
            "discarded_reason": "no generalizable rule (LLM returned null or confidence < 0.4)",
        }
    result = curator.curate(candidate, packmind=store)
    return {
        "candidate_rule": candidate,
        "submitted": result["submitted"],
        "packmind_proposal_id": result["packmind_proposal_id"],
        "discarded_reason": None if result["submitted"] else result["reason"],
    }


@mcp.tool()
async def chronos_query_playbook(topic: str, limit: int = 10) -> list:
    """Current playbook rules relevant to a topic, with their Wedge 1 evidence.

    Substring match over rule text and evidence node — Packmind's OSS API has no
    semantic search endpoint (playbook.py [D4])."""
    terms = [t for t in topic.lower().split() if t]
    scored = []
    for r in pm().list_rules():
        hay = f"{r.get('rule_text', '')} {r.get('name', '')} {r.get('evidence_node', '')}".lower()
        hits = sum(t in hay for t in terms)
        if hits or not terms:
            scored.append((hits, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


@mcp.tool()
async def chronos_propose_rule(rule_text: str, reason: str, agent_id: str) -> dict:
    """Propose a rule by hand: skips the Reflector, still passes the quality gate."""
    candidate = {
        "rule_text": rule_text,
        "confidence": 1.0,
        "evidence_node": "",
        "evidence_valid_at": "",
        "evidence_commit_context": f"manually proposed: {reason}",
        "source_trace_id": "",
        "agent_id": agent_id,
    }
    return curator.curate(candidate, packmind=pm())


@mcp.tool()
async def chronos_playbook_health() -> dict:
    """Rule counts, last proposal, and Packmind connection status."""
    try:
        return pm().health()
    except PackmindError as e:
        return {"status": "unreachable", "error": str(e), "total": 0}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
