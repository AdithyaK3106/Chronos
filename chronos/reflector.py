"""Reflector — execution trace -> candidate rule, grounded in Wedge 1 history.

The grounding is the point. "NullPointer in getActor" is a bug report; "NullPointer
in getActor, which was refactored 14 days ago and lost 2 of its 33 callers" is a
lesson. The temporal context is what the LLM reflects on.
"""

import asyncio
import json
import os
from datetime import datetime, timezone

from . import query

MODEL = lambda: os.environ.get("CHRONOS_LLM_MODEL", "openai/gpt-4o-mini")
MIN_CONFIDENCE = 0.4

# Per-node history: when the node's facts became true, and whether any were
# superseded. Mirrors query.py's window semantics.
_HISTORY = """
MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
WHERE e.group_id = $g AND (n.name = $name OR m.name = $name)
RETURN max(e.valid_at) AS last_changed,
       count(e) AS facts,
       count(e.invalid_at) AS superseded
"""

PROMPT = """You are analysing a failed AI coding-agent action to extract a durable coding standard.

EXECUTION TRACE
{trace}

TEMPORAL CONTEXT (from the codebase's bi-temporal AST graph)
{context}

Extract ONE specific, actionable coding standard rule that would have prevented this
failure. Be concrete — name the actual symbols, conditions and actions involved.
Format the rule as: IF [condition] THEN [action].

If no generalizable rule can be extracted — the failure was a one-off, an environment
problem, or too vague to generalize — return null for rule_text.

Respond with ONLY a JSON object:
{{"rule_text": "IF ... THEN ..." or null,
  "confidence": 0.0-1.0,
  "reasoning": "one sentence"}}"""


def complete(prompt, model=None):
    """Single LLM entry point. All calls route through litellm so the model stays
    configurable (CHRONOS_LLM_MODEL); tests monkeypatch this one function."""
    import litellm

    r = litellm.completion(
        model=model or MODEL(),
        messages=[{"role": "user", "content": prompt}],
        temperature=0,
        response_format={"type": "json_object"},
    )
    return r["choices"][0]["message"]["content"]


def _json(text):
    """Parse a JSON object out of an LLM response, tolerating ```json fences."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```")[1].removeprefix("json").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


async def ground(driver, group_id, nodes):
    """Wedge 1 grounding for each touched node: last change, callers/callees at
    that time, and whether a prior version was superseded."""
    out = []
    for name in nodes or []:
        rows = await query._rows(driver, _HISTORY, g=group_id, name=name)
        row = rows[0] if rows else {}
        # Kuzu hands timestamps back as datetimes, but a str survives a JSON
        # round-trip and reaches here too -- normalize before _utc.
        last = row.get("last_changed")
        if isinstance(last, str):
            try:
                last = datetime.fromisoformat(last)
            except ValueError:
                last = None
        at = query._utc(last) if last else None
        callers = await query.callers(driver, group_id, name, at)
        callees = await query.callees(driver, group_id, name, at)
        out.append({
            "node": name,
            "last_changed": at.isoformat() if at else None,
            "facts": row.get("facts", 0),
            "superseded_versions": row.get("superseded", 0),
            "callers": [c.get("name") for c in callers.get("callers", [])][:10],
            "callees": [c.get("name") for c in callees.get("callees", [])][:10],
            # Explicit no-data survives into the prompt rather than looking like
            # "no callers" — same contract the MCP tools honour (P0-4).
            "no_data": callers.get("no_data_reason"),
        })
    return out


async def reflect(driver, group_id, trace):
    """Trace -> CandidateRule dict, or None if there is no durable lesson here."""
    context = await ground(driver, group_id, trace.get("nodes_touched"))
    # complete() is a blocking litellm call; off the event loop so it doesn't
    # stall every other MCP tool call for the LLM's full response time.
    raw = await asyncio.to_thread(complete, PROMPT.format(
        trace=json.dumps(trace, indent=2),
        context=json.dumps(context, indent=2) if context else "(no nodes named in trace)",
    ))
    res = _json(raw)

    rule = res.get("rule_text")
    confidence = float(res.get("confidence") or 0.0)
    if not rule or str(rule).lower() == "null" or confidence < MIN_CONFIDENCE:
        return None

    primary = context[0] if context else {}
    return {
        "rule_text": rule,
        "confidence": confidence,
        "evidence_node": primary.get("node") or "",
        "evidence_valid_at": primary.get("last_changed") or "",
        "evidence_commit_context": _describe(primary),
        "source_trace_id": f"{trace.get('session_id', '')}:{trace.get('timestamp', '')}",
        "agent_id": trace.get("agent_id", ""),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def _describe(ctx):
    """What Wedge 1 said about the node, as one reviewable sentence."""
    if not ctx:
        return "no Wedge 1 context (trace named no nodes)"
    if ctx.get("no_data"):
        return ctx["no_data"]
    return (
        f"{ctx['node']} last changed {ctx.get('last_changed')}; "
        f"{ctx.get('facts', 0)} facts, {ctx.get('superseded_versions', 0)} superseded; "
        f"callers={ctx.get('callers') or 'none'}; callees={ctx.get('callees') or 'none'}"
    )
