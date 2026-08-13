"""Wedge 2 contract self-check: Reflector -> Curator -> Packmind.

Run: python tests/test_wedge2.py

No live LLM and no live Packmind. litellm is mocked at chronos.reflector.complete
and chronos.curator.embed; Packmind is a fake implementing the three methods the
Curator actually uses.
"""

import asyncio
import json

import tempfile
from pathlib import Path

from chronos import curator, reflector, wedge2_mcp
from chronos.playbook import EVIDENCE_MARK, Packmind, PackmindError
from chronos.store import ensure_schema

TRACE = {
    "agent_id": "agent-a",
    "session_id": "s-1",
    "action": "edited getActor to return None on cache miss",
    "outcome": "rejected",
    "error_or_correction": "PR rejected: callers assume getActor never returns None",
    "nodes_touched": ["getActor"],
    "timestamp": "2026-08-13T10:00:00+00:00",
}

RULE = "IF getActor is modified THEN preserve its non-null contract for all 33 callers"

ok = lambda m: print(f"ok  {m}")


class FakePackmind:
    """Stands in for the REST client. Same three methods curate() touches."""

    def __init__(self, existing=None, fail=False):
        self.existing = existing or []
        self.fail = fail
        self.created = []

    def list_rules(self):
        if self.fail:
            raise PackmindError("Packmind not reachable — run docs/wedge2-setup.md (refused)")
        return self.existing

    def create_standard(self, rule_text, evidence):
        if self.fail:
            raise PackmindError("Packmind not reachable — run docs/wedge2-setup.md (refused)")
        self.created.append((rule_text, evidence))
        return "std-123"

    def health(self):
        return {"status": "ok", "total": len(self.existing), "proposed": len(self.existing),
                "last_proposal": None}


def stub_llm(monkey, rule=RULE, confidence=0.9, gate=True, gate_reason="specific and grounded"):
    """One stub for both prompts — dispatch on which prompt arrived."""
    def complete(prompt, model=None):
        if "passes_gate" in prompt:
            return json.dumps({"passes_gate": gate, "reason": gate_reason})
        return json.dumps({"rule_text": rule, "confidence": confidence, "reasoning": "x"})
    monkey["complete"] = complete
    reflector.complete = complete
    curator.complete = complete


def stub_embed(similar):
    """Orthogonal vectors when not similar, identical when similar."""
    def embed(texts, model=None):
        if similar:
            return [[1.0, 0.0] for _ in texts]
        return [[1.0, 0.0]] + [[0.0, 1.0] for _ in texts[1:]]
    curator.embed = embed


class FakeDriver:
    """Minimal graph stub: history row + no callers/callees."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"last_changed": "2026-07-30T00:00:00+00:00", "facts": 5, "superseded": 2}
        ]

    async def execute_query(self, q, **kw):
        if "max(e.valid_at) AS last_changed" in q:
            return self.rows, None, None
        if "RETURN n.name AS name" in q or "RETURN m.name AS name" in q:
            return [{"name": "main", "path": "app.py", "rel": "CALLS"}], None, None
        return [], None, None


async def main():
    monkey = {}

    # --- 1. Reflector extracts a CandidateRule -----------------------------
    stub_llm(monkey)
    c = await reflector.reflect(FakeDriver(), "g", TRACE)
    assert c is not None, "expected a candidate"
    assert c["rule_text"] == RULE
    assert c["evidence_node"] == "getActor"
    assert c["evidence_valid_at"] == "2026-07-30T00:00:00+00:00"
    assert c["source_trace_id"] == "s-1:2026-08-13T10:00:00+00:00"
    assert c["agent_id"] == "agent-a"
    assert "superseded" in c["evidence_commit_context"]
    ok(f"reflector extracted a grounded rule (conf {c['confidence']}, node {c['evidence_node']})")

    # --- 2. null rule -> None ----------------------------------------------
    stub_llm(monkey, rule=None)
    assert await reflector.reflect(FakeDriver(), "g", TRACE) is None
    stub_llm(monkey, rule="null")
    assert await reflector.reflect(FakeDriver(), "g", TRACE) is None
    ok("reflector returns None on a low-signal trace (null rule)")

    # --- 3. confidence below the floor -> None -----------------------------
    stub_llm(monkey, confidence=0.39)
    assert await reflector.reflect(FakeDriver(), "g", TRACE) is None
    stub_llm(monkey, confidence=0.4)
    assert await reflector.reflect(FakeDriver(), "g", TRACE) is not None
    ok("reflector drops confidence < 0.4, keeps exactly 0.4")

    # --- 4. Curator discards a duplicate -----------------------------------
    stub_llm(monkey)
    stub_embed(similar=True)
    pmk = FakePackmind(existing=[{"name": "existing", "rule_text": "IF getActor changes THEN keep non-null"}])
    r = curator.curate(c, packmind=pmk)
    assert r["submitted"] is False and "duplicate" in r["reason"], r
    assert pmk.created == [], "duplicate must not be created"
    ok(f"curator discarded a duplicate ({r['reason']})")

    # --- 5. Curator discards on the quality gate ---------------------------
    stub_llm(monkey, gate=False, gate_reason="too vague to be actionable")
    stub_embed(similar=False)
    pmk = FakePackmind()
    r = curator.curate(c, packmind=pmk)
    assert r["submitted"] is False and "too vague" in r["reason"], r
    assert pmk.created == []
    ok(f"curator discarded on the quality gate ({r['reason']})")

    # --- 6. Curator submits a good rule ------------------------------------
    stub_llm(monkey)
    pmk = FakePackmind()
    r = curator.curate(c, packmind=pmk)
    assert r["submitted"] is True and r["packmind_proposal_id"] == "std-123", r
    text, ev = pmk.created[0]
    assert text == RULE
    assert ev["evidence_node"] == "getActor" and ev["source"] == "chronos-wedge2"
    assert ev["status"] == "proposed", "must be proposed, never auto-active"
    ok("curator submitted a good rule and returned the proposal id")

    # --- 7. Full pipeline through the MCP tool -----------------------------
    pmk = FakePackmind()
    wedge2_mcp._pm = pmk
    wedge2_mcp._driver = FakeDriver()
    res = await wedge2_mcp.chronos_capture_lesson(TRACE)
    assert res["submitted"] is True and res["packmind_proposal_id"] == "std-123"
    assert res["candidate_rule"]["evidence_node"] == "getActor"
    assert res["discarded_reason"] is None
    ok("full pipeline: trace -> candidate -> Packmind proposal")

    stub_llm(monkey, rule=None)
    res = await wedge2_mcp.chronos_capture_lesson(TRACE)
    assert res["submitted"] is False and res["candidate_rule"] is None
    assert "no generalizable rule" in res["discarded_reason"]
    ok("pipeline reports a discarded low-signal trace, does not crash")

    # --- 8. query_playbook carries evidence context ------------------------
    wedge2_mcp._pm = FakePackmind(existing=[
        {"name": "auth", "rule_text": "IF auth token expires THEN refresh",
         "evidence_node": "getActor", "evidence_valid_at": "2026-07-30T00:00:00+00:00"},
        {"name": "db", "rule_text": "IF getDb is called THEN reuse the pool",
         "evidence_node": "getDb", "evidence_valid_at": "2026-07-01T00:00:00+00:00"},
    ])
    rows = await wedge2_mcp.chronos_query_playbook("auth token")
    assert rows and rows[0]["evidence_node"] == "getActor", rows
    assert rows[0]["evidence_valid_at"] == "2026-07-30T00:00:00+00:00"
    ok("query_playbook ranked by topic and kept evidence context attached")

    # --- 9. Packmind unreachable -> loud, never a silent discard ------------
    stub_llm(monkey)
    wedge2_mcp._pm = FakePackmind(fail=True)
    wedge2_mcp._driver = FakeDriver()
    try:
        await wedge2_mcp.chronos_capture_lesson(TRACE)
        raise AssertionError("unreachable Packmind must raise, not discard")
    except PackmindError as e:
        assert "docs/wedge2-setup.md" in str(e)
    ok("unreachable Packmind raises with the setup hint, trace not silently dropped")

    h = await wedge2_mcp.chronos_playbook_health()
    assert h["status"] == "ok"  # FakePackmind.health does not fail
    try:
        Packmind(url="", key="")
        raise AssertionError("missing config must raise")
    except PackmindError as e:
        assert "PACKMIND_API_URL" in str(e)
    ok("missing PACKMIND_API_URL/KEY fails loudly at construction")

    # evidence survives the description round-trip (playbook.py's documented gap)
    ev = {"evidence_node": "getActor", "evidence_valid_at": "2026-07-30T00:00:00+00:00"}
    desc = f"{RULE}\n\n{EVIDENCE_MARK}\n{json.dumps(ev)}"
    body, _, tail = desc.partition(EVIDENCE_MARK)
    assert body.strip() == RULE and json.loads(tail.strip())["evidence_node"] == "getActor"
    ok("evidence block round-trips through the standard description")

    # --- 10. Round-trip: evidence_node resolves in a real Wedge 1 graph ----
    await roundtrip()

    print("\nALL PASS")


async def roundtrip(repo=None):
    """A submitted rule's evidence_node must name a real symbol in the Wedge 1
    graph. A rule grounded in a symbol the graph cannot resolve is not grounded —
    the evidence would be decoration. Skipped (not failed) without a built indexer.
    """
    import os
    import shutil

    from chronos import indexer, query
    from chronos.store import open_driver
    from chronos.sync import Syncer

    if indexer.binary_path() is None:
        print(f"skip round-trip: vendored indexer not built ({indexer.BUILD_CMD})")
        return

    repo = repo or str(Path(__file__).resolve().parents[1])
    tmp = tempfile.mkdtemp()
    os.environ["CHRONOS_DB"] = os.path.join(tmp, "w2rt.kz")
    G = "wedge2-roundtrip"

    nodes, edges = indexer.index_repo_graph(repo)
    assert nodes, f"indexer returned no nodes for {repo}"

    drv = open_driver()
    await ensure_schema(drv)
    await Syncer(drv, G).sync(nodes, edges)

    # Take real indexed symbols as if the Reflector had grounded on them, then
    # resolve each the way chronos_query_playbook's consumer would.
    sample = [n["name"] for n in list(nodes.values())[:40] if n.get("name")]
    assert sample, "no named symbols to ground on"

    unresolved = []
    for name in sample:
        res = await query.callers(drv, G, name)
        if "is not present in the graph" in (res.get("no_data_reason") or ""):
            unresolved.append(name)

    assert not unresolved, (
        f"{len(unresolved)}/{len(sample)} evidence nodes did not resolve: {unresolved[:5]}"
    )
    ok(f"round-trip: {len(sample)}/{len(sample)} evidence nodes resolve in the Wedge 1 graph")

    await drv.close()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
