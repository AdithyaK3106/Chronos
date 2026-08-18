"""MCP surface for the self-improving playbook (Wedge 2).

capture_lesson is what an agent calls after being corrected; query_playbook is what
it calls before starting work. Packmind is the store — Chronos only reflects,
curates, and reads back.
"""

import asyncio
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import groups, curator, reflector
from .curator import _get_submission_path
from .playbook import Packmind, PackmindError, PackmindNotConfigured
from .rule_submission import resolve_repo_path

GROUP = groups.resolve(os.environ.get("CHRONOS_GROUP_ID"),
                        os.environ.get("CHRONOS_REPO_PATH"))
mcp = FastMCP("chronos-playbook")

_pm = None


async def driver():
    """Delegates to wedge1_mcp, which owns the single process-wide driver.

    Each wedge used to keep its own `_driver` global. In the unified server all
    four wedges live in ONE process, and Kuzu allows one holder per process --
    so calling a Wedge 2 tool after a Wedge 1 tool made the second wedge block
    on a lock the first already held, inside the same process. No error, no
    timeout, ~0% CPU: the server just stopped answering.
    """
    from .wedge1_mcp import driver as _shared
    return await _shared()


def pm():
    """The Packmind client, or None when the git-native path is active.

    Returns None rather than raising so capture_lesson works out of the box
    with no Packmind anywhere. A configured-but-unreachable store still
    raises — that is an outage, not a routing decision."""
    global _pm
    if _pm is not None:
        return _pm  # explicitly injected client always wins over env routing
    if _get_submission_path() != "packmind":
        return None
    if _pm is None:
        try:
            _pm = Packmind()
        except PackmindNotConfigured:
            return None
    return _pm


@mcp.tool()
async def chronos_capture_lesson(trace: dict) -> dict:
    """Learn a coding standard from a failed agent action.

    trace: {agent_id, session_id, action, outcome, error_or_correction,
            nodes_touched: [qualified_name], timestamp}
    Runs Reflector (grounded in the temporal graph) then Curator, and on success
    creates an unpublished Packmind standard for a human to approve."""
    store = pm()  # None -> git-native; raises loudly if configured but broken
    path = _get_submission_path()
    candidate = await reflector.reflect(await driver(), GROUP, trace)
    if candidate is None:
        return {
            "candidate_rule": None,
            "submitted": False,
            "packmind_proposal_id": None,
            "discarded_reason": "no generalizable rule (LLM returned null or confidence < 0.4)",
            "submission_path": path,
            "packmind_configured": bool(os.environ.get("PACKMIND_API_URL")),
        }
    # curate() makes blocking litellm calls; off the event loop or every other
    # MCP tool call stalls behind this one for the LLM's full response time.
    result = await asyncio.to_thread(curator.curate, candidate, packmind=store)
    return {
        "candidate_rule": candidate,
        "submitted": result["submitted"],
        "packmind_proposal_id": result["packmind_proposal_id"],
        "discarded_reason": None if result["submitted"] else result["reason"],
        "submission_path": result.get("submission_path", path),
        "packmind_configured": bool(os.environ.get("PACKMIND_API_URL")),
        # git-native only; absent on the Packmind path.
        "branch": result.get("branch"),
        "pr_url": result.get("pr_url"),
        "rule_id": result.get("rule_id"),
    }


@mcp.tool()
async def chronos_query_playbook(topic: str, limit: int = 10) -> list:
    """Current playbook rules relevant to a topic, with their Wedge 1 evidence.

    Substring match over rule text and evidence node — Packmind's OSS API has no
    semantic search endpoint (playbook.py [D4])."""
    terms = [t for t in topic.lower().split() if t]
    scored = []
    for r in curator._existing_rules(pm()):
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
    result = await asyncio.to_thread(curator.curate, candidate, packmind=pm())
    return {
        **result,
        "submission_path": result.get("submission_path", _get_submission_path()),
        "packmind_configured": bool(os.environ.get("PACKMIND_API_URL")),
    }


@mcp.tool()
async def chronos_playbook_health() -> dict:
    """Rule counts, last proposal, and which distribution path is active."""
    repo_path = resolve_repo_path(None)
    meta = {
        "rule_backend": _get_submission_path(),
        "packmind_url": os.environ.get("PACKMIND_API_URL", None),
        "git_native_rules_dir": str(Path(repo_path) / ".chronos" / "rules"),
    }
    store = pm()
    if store is None:
        from . import rule_store
        c = rule_store.counts()
        return {"status": "ok", "total": c["total"],
                "proposed": c.get("proposed", 0),
                "blocking": c["blocking"], "warn_only": c["warn_only"], **meta}
    try:
        return {**store.health(), **meta}
    except PackmindError as e:
        return {"status": "unreachable", "error": str(e), "total": 0, **meta}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
