"""MCP surface for CI enforcement (Wedge 4).

generate_rule turns a Wedge 2 playbook rule into a CI check; enforce runs the
active checks over a file. Every generated rule starts warn-only -- promotion to
blocking is an explicit human act, and is refused for rules that failed
detectability.
"""

import os
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from . import groups, detectability, enforcer, ledger, rule_generator, rule_store
from .store import open_driver

GROUP = groups.resolve(os.environ.get("CHRONOS_GROUP_ID"),
                        os.environ.get("CHRONOS_REPO_PATH"))
mcp = FastMCP("chronos-enforce")

_driver = None


async def driver():
    global _driver
    if _driver is None:
        _driver = open_driver()
    return _driver


@mcp.tool()
async def chronos_generate_rule(rule_text: str, language: str, rule_id: str,
                                evidence_node: str = None) -> dict:
    """Turn a plain-English playbook rule into a validated ast-grep CI check.

    Pipeline: LLM generation -> detectability -> stored as warn-only."""
    gen = rule_generator.generate(rule_text, language, rule_id, evidence_node)
    if not gen["automatable"]:
        return {"rule_id": rule_id, "automatable": False,
                "detectability_passed": False, "false_positive_risk": False,
                "status": None, "reason": gen["not_automatable_reason"]}

    det = detectability.validate(rule_id, gen["yaml_pattern"], language)
    rule_store.upsert_rule(rule_id, language, rule_text, gen["yaml_pattern"], det)
    stored = rule_store.get_rule(rule_id) or {}
    return {"rule_id": rule_id, "automatable": True,
            "detectability_passed": det["passed"],
            "false_positive_risk": det["false_positive_risk"],
            "status": stored.get("status"), "reason": det["details"]}


@mcp.tool()
async def chronos_enforce(file_path: str, language: str, agent_id: str = None,
                          session_id: str = None) -> list:
    """Run all active rules for this language against a file.

    Callers gate on any verdict == "block"."""
    return await enforcer.enforce(file_path, language, agent_id, session_id,
                                  driver=await driver(), group_id=GROUP)


@mcp.tool()
async def chronos_promote_rule(rule_id: str, promoted_by: str) -> dict:
    """Promote a validated rule from warn-only to blocking.

    Refuses rules that failed detectability."""
    return rule_store.promote_to_blocking(rule_id, promoted_by)


@mcp.tool()
async def chronos_list_rules(language: str = None) -> list:
    """All enforcement rules with status, detectability, and false-positive risk."""
    return [{"rule_id": r["rule_id"], "language": r["language"],
             "rule_text": r["rule_text"], "status": r["status"],
             "detectability_passed": bool(r["detectability_passed"]),
             "false_positive_risk": bool(r["false_positive_risk"]),
             "created_at": r["created_at"], "promoted_at": r["promoted_at"],
             "promoted_by": r["promoted_by"]}
            for r in rule_store.get_active_rules(language)]


@mcp.tool()
async def chronos_rule_report(since_days: int = 30) -> dict:
    """Enforcement audit: what got blocked, by which rule, on which node.

    This reports across ALL rules -- it takes a time window, not a rule id.
    (The name invites passing one, so a non-numeric argument is reported as a
    structured error rather than raising TypeError at the caller: an LLM
    guessing the wrong argument should get an answer it can act on.)

    Blocks come from Wedge 3's provenance_events (action='blocked_by_ci'), which
    is why they are attributable to an agent and a session at all."""
    try:
        since_days = int(since_days)
    except (TypeError, ValueError):
        return {"error": f"since_days must be a number of days, got {since_days!r}. "
                         "This tool reports on all rules; it takes no rule id."}
    if since_days <= 0:
        return {"error": f"since_days must be positive, got {since_days}"}
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    con = rule_store.connect()
    try:
        rows = con.execute(
            "SELECT node_id, reason FROM provenance_events "
            "WHERE action='blocked_by_ci' AND timestamp >= ?", (since,)).fetchall()
        by_rule, by_node = {}, {}
        for r in rows:
            # reason is "rule <id>: <text>" (enforcer._result)
            rid = r["reason"].split(":", 1)[0].replace("rule ", "").strip() \
                if r["reason"].startswith("rule ") else "unknown"
            by_rule[rid] = by_rule.get(rid, 0) + 1
            by_node[r["node_id"]] = by_node.get(r["node_id"], 0) + 1
        top = lambda d, k: [{k: n, "count": c} for n, c in
                            sorted(d.items(), key=lambda x: -x[1])[:10]]
        return {
            "total_checks": len(rows),
            "blocks": len(rows),
            # ponytail: warns/passes are not persisted -- only blocks are
            # stamped into the ledger. Counting them would mean a second write
            # path on every clean CI run, for a number nobody acts on.
            "warns": None,
            "passes": None,
            "top_violated_rules": top(by_rule, "rule_id"),
            "top_blocked_nodes": top(by_node, "qualified_name"),
            "period_days": since_days,
            "note": "blocks are sourced from provenance_events; warn/pass "
                    "verdicts are returned live by chronos_enforce and not persisted",
        }
    finally:
        con.close()


def main():
    mcp.run()


if __name__ == "__main__":
    main()
