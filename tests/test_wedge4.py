"""Wedge 4 contract self-check: generate -> validate -> enforce -> stamp.

Run: python tests/test_wedge4.py

Mocks the LLM (chronos.reflector.complete), ast-grep (enforcer.scan) and OPA
(enforcer.opa_eval). Neither binary nor any API key is needed. Test 14 uses the
real ast-grep and OPA when present, and skips cleanly when they are not.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from chronos import (detectability, enforcer, ledger, rule_generator,
                     rule_store, wedge4_mcp)

ok = lambda m: print(f"ok  {m}")

GOOD_YAML = """id: no-direct-createclient
language: typescript
severity: warning
message: Use the factory instead of calling createClient directly
rule:
  pattern: createClient($$$ARGS)
"""

MATCH = {
    "text": "createClient({url: 'x'})",
    "file": "demo.ts",
    "range": {"start": {"line": 1, "column": 10}},
    "ruleId": "no-direct-createclient",
    "metaVariables": {"single": {}, "multi": {}},
}


def stub_llm(mapping):
    """Dispatch canned LLM responses by a substring of the prompt."""
    def complete(prompt, model=None):
        for key, val in mapping.items():
            if key in prompt:
                return val
        return mapping.get("*", "")
    rule_generator.complete = complete
    detectability.complete = complete


def stub_scan(result):
    """result: (matches, exit_code, stderr) or a callable taking (rule, target)."""
    calls = []

    def scan(rule_path, target):
        calls.append((str(rule_path), str(target)))
        return result(rule_path, target) if callable(result) else result
    enforcer.scan = scan
    detectability.scan = scan
    return calls


def stub_opa(verdict, reason="stubbed"):
    seen = []

    def opa_eval(payload):
        seen.append(payload)
        v = verdict(payload) if callable(verdict) else verdict
        return {"verdict": v, "reason": reason}
    enforcer.opa_eval = opa_eval
    return seen


class FakeDriver:
    """Wedge 1 stub: `deprecated` names have all their facts closed."""

    def __init__(self, deprecated=()):
        self.deprecated = set(deprecated)

    async def execute_query(self, q, **kw):
        name = kw.get("name")
        if name in self.deprecated:
            return [{"facts": 3, "closed": 3,
                     "last_closed": "2026-07-30T00:00:00+00:00"}], None, None
        return [{"facts": 2, "closed": 0, "last_closed": None}], None, None


def fresh_db():
    """A rule/ledger DB isolated per test run."""
    tmp = tempfile.mkdtemp()
    os.environ["CHRONOS_LEDGER"] = str(Path(tmp) / "ledger.db")
    con = rule_store.connect(Path(tmp) / "ledger.db")
    return con


async def main():
    con = fresh_db()

    # --- 1. generator produces YAML for an automatable rule ----------------
    stub_llm({"ast-grep structural search": f"```yaml\n{GOOD_YAML}```"})
    g = rule_generator.generate("Never call createClient directly", "typescript",
                                "r1", evidence_node="createClient")
    assert g["automatable"] is True
    assert "pattern: createClient" in g["yaml_pattern"], g
    assert g["raw_llm_output"], "raw output must always be preserved"
    assert (rule_generator.RULES_DIR / "r1.yml").exists(), "YAML must be written to disk"
    ok("rule_generator produced valid YAML and stored it")

    # --- 2. NOT_AUTOMATABLE ------------------------------------------------
    stub_llm({"ast-grep structural search":
              "NOT_AUTOMATABLE: naming quality is not a structural property"})
    g2 = rule_generator.generate("Use clear variable names", "typescript", "r2")
    assert g2["automatable"] is False and g2["yaml_pattern"] is None
    assert "NAMING QUALITY" in g2["not_automatable_reason"].upper(), g2
    ok(f"rule_generator returned NOT_AUTOMATABLE ({g2['not_automatable_reason'][:40]}...)")

    # --- 3. CHECK A fails on invalid YAML ----------------------------------
    stub_scan(([], enforcer.EXIT_RULE_PARSE, "Cannot parse rule"))
    d = detectability.validate("r3", "id: bad\nrule:\n  pattern: '(((('", "typescript")
    assert d["syntax_valid"] is False and d["passed"] is False
    assert "CHECK A failed" in d["details"], d
    ok("detectability CHECK A rejected unparseable YAML (exit 8)")

    # --- 4. CHECK B fails when the rule misses its own example -------------
    stub_llm({"SHOULD match": "const a = createClient();", "should NOT": "const b = 1;"})
    stub_scan(([], 0, ""))  # parses fine, but matches nothing
    d = detectability.validate("r4", GOOD_YAML, "typescript")
    assert d["syntax_valid"] is True and d["catches_true_positive"] is False
    assert d["passed"] is False and "CHECK B failed" in d["details"], d
    ok("detectability CHECK B rejected a rule that misses its own positive example")

    # --- 5. detectability passes; negative example flags FP risk -----------
    # Both snippets contain createClient, so the rule fires on the negative one
    # too -- exactly the over-broad pattern the FP flag exists to catch.
    stub_llm({"SHOULD match": "const a = createClient();",
              "should NOT": "const b = createClient.name;"})
    stub_scan(lambda rule, target: (([MATCH], 0, "")
                                    if "createClient" in Path(target).read_text(encoding="utf-8")
                                    else ([], 0, "")))
    d = detectability.validate("r5", GOOD_YAML, "typescript")
    assert d["passed"] is True and d["catches_true_positive"] is True
    assert d["false_positive_risk"] is True, "fired on the negative example too"
    ok("detectability passed and flagged false-positive risk on the negative example")

    stub_llm({"SHOULD match": "const a = createClient();",
              "should NOT": "const b = factory.make();"})
    d_clean = detectability.validate("r5b", GOOD_YAML, "typescript")
    assert d_clean["passed"] is True and d_clean["false_positive_risk"] is False
    ok("detectability passed cleanly when the negative example does not fire")

    # --- 6. block: match + graph confirms + blocking -----------------------
    rule_store.upsert_rule("rb", "typescript", "no direct createClient", GOOD_YAML,
                           {"passed": True, "false_positive_risk": False}, con=con)
    rule_store.promote_to_blocking("rb", "human", con=con)
    assert rule_store.get_rule("rb", con=con)["status"] == "blocking"

    stub_scan(([MATCH], 0, ""))
    seen = stub_opa(lambda p: "block" if (p["rule_status"] == "blocking"
                                          and p["deprecated_in_graph"]) else "warn")
    res = await enforcer.enforce("demo.ts", "typescript", agent_id="agent-a",
                                 session_id="s1",
                                 driver=FakeDriver(deprecated={"createClient"}),
                                 group_id="g", con=con)
    assert len(res) == 1 and res[0]["verdict"] == "block", res
    assert res[0]["deprecated_since"] == "2026-07-30T00:00:00+00:00"
    assert seen[0]["deprecated_in_graph"] is True
    ok("enforcer: match + graph-confirmed deprecation + blocking -> block")

    # --- 10. block was stamped into Wedge 3 -------------------------------
    assert res[0]["provenance_event_id"], "block must be stamped into the ledger"
    ev = con.execute("SELECT * FROM provenance_events WHERE action='blocked_by_ci'").fetchall()
    assert len(ev) == 1 and ev[0]["agent_id"] == "agent-a"
    assert ev[0]["node_id"] == "createClient", dict(ev[0])
    assert "rule rb" in ev[0]["reason"], ev[0]["reason"]
    ok(f"block stamped into provenance_events (id={res[0]['provenance_event_id']}, "
       f"node={ev[0]['node_id']}, agent={ev[0]['agent_id']})")

    # --- 7. blocking rule, graph does NOT confirm -> warn ------------------
    res = await enforcer.enforce("demo.ts", "typescript", agent_id="agent-a",
                                 session_id="s1", driver=FakeDriver(deprecated=set()),
                                 group_id="g", con=con)
    assert res[0]["verdict"] == "warn", res
    assert res[0]["deprecated_since"] is None
    assert res[0]["provenance_event_id"] is None, "warns must not be stamped"
    n = con.execute("SELECT count(*) c FROM provenance_events "
                    "WHERE action='blocked_by_ci'").fetchone()["c"]
    assert n == 1, f"warn wrote a provenance event ({n} total)"
    ok("enforcer: blocking rule but graph does not confirm -> warn, nothing stamped")

    # --- 8. warn-only rule -> warn regardless of the graph -----------------
    con.execute("DELETE FROM enforcement_rules")
    rule_store.upsert_rule("rw", "typescript", "warn only", GOOD_YAML,
                           {"passed": True, "false_positive_risk": False}, con=con)
    res = await enforcer.enforce("demo.ts", "typescript",
                                 driver=FakeDriver(deprecated={"createClient"}),
                                 group_id="g", con=con)
    assert res[0]["verdict"] == "warn" and res[0]["rule_status"] == "warn-only-validated"
    ok("enforcer: warn-only rule -> warn even though the graph confirms deprecation")

    # --- 9. no match -> pass ----------------------------------------------
    stub_scan(([], 0, ""))
    res = await enforcer.enforce("clean.ts", "typescript", driver=FakeDriver(),
                                 group_id="g", con=con)
    assert res[0]["verdict"] == "pass" and res[0]["matched_node"] is None
    ok("enforcer: no ast-grep match -> pass")

    # --- 11/12. promotion gate --------------------------------------------
    rule_store.upsert_rule("bad", "typescript", "unvalidated", GOOD_YAML,
                           {"passed": False, "false_positive_risk": False}, con=con)
    r = rule_store.promote_to_blocking("bad", "human", con=con)
    assert r["promoted"] is False and "cannot promote" in r["reason"], r
    assert rule_store.get_rule("bad", con=con)["status"] == "warn-only-unvalidated"
    ok(f"promote refused an unvalidated rule ({r['reason'][:45]}...)")

    rule_store.upsert_rule("good", "typescript", "validated", GOOD_YAML,
                           {"passed": True, "false_positive_risk": False}, con=con)
    r = rule_store.promote_to_blocking("good", "human", con=con)
    assert r["promoted"] is True and r["status"] == "blocking" and r["promoted_at"]
    assert rule_store.get_rule("good", con=con)["promoted_by"] == "human"
    ok("promote succeeded for a validated rule; status -> blocking")

    # a promoted rule must not be silently demoted by regeneration
    rule_store.upsert_rule("good", "typescript", "validated", GOOD_YAML,
                           {"passed": True, "false_positive_risk": True}, con=con)
    assert rule_store.get_rule("good", con=con)["status"] == "blocking"
    ok("regenerating a promoted rule keeps it blocking (no silent demotion)")

    # --- 13. rule report ---------------------------------------------------
    for node, rid in [("getActor", "rb"), ("getActor", "rb"), ("getDb", "good")]:
        ledger.log_event(con, node_id=node, agent_id="agent-a", session_id="s",
                         action="blocked_by_ci", reason=f"rule {rid}: blocked")
    con.commit()
    rep = await _report_with(con, 30)
    assert rep["blocks"] == 4, rep  # 1 from the enforce test + 3 here
    assert rep["top_violated_rules"][0]["rule_id"] == "rb", rep["top_violated_rules"]
    assert rep["top_blocked_nodes"][0]["qualified_name"] == "getActor", rep["top_blocked_nodes"]
    assert rep["top_blocked_nodes"][0]["count"] == 2
    ok(f"rule_report: {rep['blocks']} blocks, top rule={rep['top_violated_rules'][0]['rule_id']}, "
       f"top node={rep['top_blocked_nodes'][0]['qualified_name']}")

    con.close()

    # --- 14. live integration on a real file -------------------------------
    await live_integration()

    print("\nALL PASS")


async def _report_with(con, days):
    """chronos_rule_report's logic against a supplied connection (the tool opens
    its own; this keeps the test on the temp DB)."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = con.execute("SELECT node_id, reason FROM provenance_events "
                       "WHERE action='blocked_by_ci' AND timestamp >= ?", (since,)).fetchall()
    by_rule, by_node = {}, {}
    for r in rows:
        rid = r["reason"].split(":", 1)[0].replace("rule ", "").strip()
        by_rule[rid] = by_rule.get(rid, 0) + 1
        by_node[r["node_id"]] = by_node.get(r["node_id"], 0) + 1
    top = lambda d, k: [{k: n, "count": c} for n, c in
                        sorted(d.items(), key=lambda x: -x[1])[:10]]
    return {"blocks": len(rows), "total_checks": len(rows),
            "top_violated_rules": top(by_rule, "rule_id"),
            "top_blocked_nodes": top(by_node, "qualified_name"),
            "period_days": days}


async def live_integration():
    """Real ast-grep + real OPA over a real file. No LLM: the YAML is fixed, so
    this exercises the tools and the wiring, not the model. Skips if either
    binary is absent -- the suite must not require them."""
    import importlib

    importlib.reload(enforcer)  # restore the real scan/opa_eval
    if enforcer.ast_grep_version() is None or enforcer.opa_version() is None:
        print("skip live integration: ast-grep and/or opa not on PATH")
        return
    importlib.reload(rule_store)

    tmp = tempfile.mkdtemp()
    os.environ["CHRONOS_LEDGER"] = str(Path(tmp) / "l.db")
    con = rule_store.connect(Path(tmp) / "l.db")

    src = Path(tmp) / "real.ts"
    src.write_text("import {createClient} from './db';\n"
                   "const a = createClient({url: 'x'});\n"
                   "function ok() { return factory.make(); }\n", encoding="utf-8")

    enforcer.RULES_DIR = Path(tmp) / "rules"
    enforcer.RULES_DIR.mkdir(parents=True, exist_ok=True)
    (enforcer.RULES_DIR / "live1.yml").write_text(GOOD_YAML.replace(
        "id: no-direct-createclient", "id: live1"), encoding="utf-8")
    rule_store.upsert_rule("live1", "typescript", "no direct createClient",
                           GOOD_YAML, {"passed": True}, con=con)

    res = await enforcer.enforce(str(src), "typescript", driver=None,
                                 group_id="g", con=con)
    verdicts = [r["verdict"] for r in res]
    assert "warn" in verdicts, f"expected a warn from the real toolchain, got {res}"
    hit = next(r for r in res if r["verdict"] == "warn")
    assert "createClient" in (hit["matched_node"] or "")
    ok(f"live: real ast-grep matched {hit['matched_node']!r}, real OPA -> "
       f"{hit['verdict']} ({hit['message'][:60]})")
    con.close()


if __name__ == "__main__":
    asyncio.run(main())
