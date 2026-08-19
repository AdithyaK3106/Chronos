"""Unification contract: one server, one database, three best-effort triggers.

Run: python tests/test_unification.py

Covers the seams the refactor created -- tool aggregation, the trigger kill
switch, trigger isolation (a failing trigger must not affect its caller),
thread-local DB reuse, and the legacy migration.
"""

import asyncio
import logging
import os
import sqlite3
import threading
import subprocess
import sys
import tempfile
from pathlib import Path

from chronos import db, enforcer, ledger, rule_store, server, triggers

ok = lambda m: print(f"ok  {m}")
ROOT = Path(__file__).resolve().parents[1]


class Capture(logging.Handler):
    """Collects records from chronos.triggers so we can assert on them."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def text(self):
        return "\n".join(m for _, m in self.records)

    def __enter__(self):
        self.log = logging.getLogger("chronos.triggers")
        self._prev = self.log.level
        self.log.setLevel(logging.INFO)
        self.log.addHandler(self)
        return self

    def __exit__(self, *a):
        self.log.removeHandler(self)
        self.log.setLevel(self._prev)


def isolate_db():
    """Point every SQLite path at a fresh temp file."""
    tmp = Path(tempfile.mkdtemp())
    os.environ.pop("CHRONOS_LEDGER", None)
    os.environ["CHRONOS_SQLITE"] = str(tmp / "chronos.db")
    db.reset()
    db._migrated.clear()
    return tmp


async def main():
    os.environ["CHRONOS_AUTO_TRIGGERS"] = "true"

    # --- 1. every wedge's tools are on the one server ----------------------
    tools = {t.name for t in await server.mcp.list_tools()}
    from chronos import wedge1_mcp, wedge2_mcp, wedge3_mcp, wedge4_mcp
    expected = {
        "as_of_callers", "as_of_callees", "as_of_impact", "as_of_diff", "what_changed", "index_health",
        "chronos_acquire_lock", "chronos_release_lock", "chronos_check_conflicts",
        "chronos_log_provenance", "chronos_who_touched",
        "chronos_capture_lesson", "chronos_query_playbook", "chronos_propose_rule",
        "chronos_playbook_health",
        "chronos_generate_rule", "chronos_enforce", "chronos_promote_rule",
        "chronos_list_rules", "chronos_rule_report",
        # server.py-only: reports in-flight calls, not owned by any one wedge.
        "chronos_mcp_status",
    }
    assert tools == expected, f"missing {expected - tools}, extra {tools - expected}"
    per_wedge = 0
    for m in (wedge1_mcp, wedge2_mcp, wedge3_mcp, wedge4_mcp):
        per_wedge += len(await m.mcp.list_tools())
    server_only = len(tools) - per_wedge
    assert server_only == 1, f"unified={len(tools)} but wedges total {per_wedge} (expected exactly 1 server-only tool: chronos_mcp_status)"
    assert server.mcp.name == "chronos"
    ok(f"unified server exposes all {len(tools)} tools: {per_wedge} from 4 wedges + {server_only} server-only")

    # --- 2. CHRONOS_AUTO_TRIGGERS=false disables every trigger -------------
    os.environ["CHRONOS_AUTO_TRIGGERS"] = "false"
    assert triggers.enabled() is False
    called = []
    import chronos.reflector as reflector
    orig_reflect = reflector.reflect

    async def spy(*a, **k):
        called.append(a)
        return None
    reflector.reflect = spy
    try:
        assert triggers.on_block("r", "m", "n", "a", "s") is None
        assert triggers.on_deprecation("getActor") is None
        assert triggers.on_conflict("n", "a", "b") is None
        assert not called, "reflector must not be called when triggers are off"
    finally:
        reflector.reflect = orig_reflect
    for v in ("0", "no", "off", "FALSE"):
        os.environ["CHRONOS_AUTO_TRIGGERS"] = v
        assert triggers.enabled() is False, v
    os.environ["CHRONOS_AUTO_TRIGGERS"] = "true"
    assert triggers.enabled() is True
    ok("CHRONOS_AUTO_TRIGGERS=false disables all three triggers (0/no/off honoured)")

    # --- 3. block -> trigger 1 -> reflector gets the right trace -----------
    seen = {}

    async def fake_reflect(driver, group_id, trace):
        seen.update(trace)
        return {"rule_text": "IF x THEN y", "confidence": 0.9,
                "evidence_node": trace["nodes_touched"][0] if trace["nodes_touched"] else ""}
    reflector.reflect = fake_reflect
    import chronos.curator as curator
    orig_curate = curator.curate
    curated = []
    curator.curate = lambda c, **k: curated.append(c) or {"submitted": True}
    try:
        # run_block_sync is the synchronous body; on_block now dispatches it
        # to a daemon thread, so assertions on its effects use this directly.
        triggers.run_block_sync(rule_id="r-42", message="deprecated pattern",
                          matched_qualified_name="getActor", agent_id="agent-a",
                          session_id="s-9", rule_text="never call getActor")
        assert seen["agent_id"] == "agent-a" and seen["session_id"] == "s-9"
        assert seen["outcome"] == "rejected"
        assert seen["nodes_touched"] == ["getActor"]
        assert "r-42" in seen["action"]
        assert "deprecated pattern" in seen["error_or_correction"]
        assert seen["timestamp"], "trace needs a timestamp"
        assert len(curated) == 1, "curator should receive the candidate"
        ok("block -> reflector received a well-formed trace, curator got the candidate")

        # defaults when the CI runner supplies no identity
        seen.clear()
        triggers.run_block_sync("r-1", "m", None, None, None)
        assert seen["agent_id"] == "unknown" and seen["session_id"] == "ci"
        assert seen["nodes_touched"] == []
        ok("block with no agent/session defaults to unknown/ci, empty nodes_touched")
    finally:
        curator.curate = orig_curate

    # --- 4. a throwing reflector must not disturb the block ----------------
    async def boom(*a, **k):
        raise RuntimeError("reflector exploded")
    reflector.reflect = boom
    assert triggers.run_block_sync("r-x", "m", "n", "a", "s") is None, "must swallow"
    ok("reflector exception swallowed by the trigger (returns None)")

    # end-to-end: the verdict survives a broken trigger
    tmp = isolate_db()
    con = rule_store.connect()
    rule_store.upsert_rule("rb", "typescript", "no direct createClient",
                           "id: rb\nlanguage: typescript\nrule:\n  pattern: createClient($$$A)\n",
                           {"passed": True}, con=con)
    rule_store.promote_to_blocking("rb", "human", con=con)
    orig_scan, orig_opa = enforcer.scan, enforcer.opa_eval
    enforcer.scan = lambda r, t: ([{"text": "createClient()", "file": "d.ts",
                                    "range": {"start": {"line": 0}},
                                    "metaVariables": {"single": {}}}], 0, "")
    enforcer.opa_eval = lambda p: {"verdict": "block", "reason": "confirmed"}

    class Dep:
        async def execute_query(self, q, **kw):
            return [{"facts": 2, "closed": 2, "last_closed": "2026-07-30T00:00:00+00:00"}], None, None
    try:
        res = await enforcer.enforce("d.ts", "typescript", agent_id="agent-a",
                                     session_id="s1", driver=Dep(), group_id="g", con=con)
        assert res[0]["verdict"] == "block", res
        assert res[0]["provenance_event_id"], "block must still be stamped"
        ok("enforcement verdict stands (block, stamped) despite the reflector throwing")
    finally:
        enforcer.scan, enforcer.opa_eval = orig_scan, orig_opa
        reflector.reflect = orig_reflect
        con.close()

    # --- 4b. trigger 1 must not stall the enforcement path ------------------
    # Validation measured 5,099ms per blocking verdict with the Reflector inline
    # vs 134ms without. This pins the fix: a slow Reflector must not be paid for
    # by the caller.
    import time
    from unittest.mock import patch

    slow_started = threading.Event()
    slow_done = threading.Event()

    async def slow_reflect(*a, **k):
        slow_started.set()
        await asyncio.sleep(3)  # stands in for a live LLM round-trip
        slow_done.set()
        return None

    con = rule_store.connect()
    rule_store.upsert_rule("rb2", "typescript", "no direct createClient",
                           "id: rb2\nlanguage: typescript\nrule:\n  pattern: createClient($$$A)\n",
                           {"passed": True}, con=con)
    rule_store.promote_to_blocking("rb2", "human", con=con)
    enforcer.scan = lambda r, t: ([{"text": "createClient()", "file": "d.ts",
                                    "range": {"start": {"line": 0}},
                                    "metaVariables": {"single": {}}}], 0, "")
    enforcer.opa_eval = lambda p: {"verdict": "block", "reason": "confirmed"}
    try:
        with patch("chronos.reflector.reflect", slow_reflect):
            t0 = time.perf_counter()
            res = await enforcer.enforce("d.ts", "typescript", agent_id="agent-a",
                                         session_id="s1", driver=Dep(), group_id="g",
                                         con=con)
            elapsed = time.perf_counter() - t0
        assert res[0]["verdict"] == "block", res
        assert elapsed < 2.0, (
            f"enforce() took {elapsed:.2f}s with a 3s Reflector — trigger 1 is "
            "still blocking the enforcement path")
        assert slow_started.wait(timeout=5), "trigger 1 never actually ran"
        assert not slow_done.is_set(), "enforce() waited for the Reflector to finish"
        ok(f"trigger 1 is fire-and-forget: enforce() returned a block in "
           f"{elapsed*1000:.0f}ms while a 3s Reflector ran in the background")
        assert triggers.drain(timeout=10) == 0, "background trigger thread did not finish"
        assert slow_done.is_set(), "drain() returned before the Reflector completed"
        ok("drain() waits for the in-flight trigger thread to complete")
    finally:
        enforcer.scan, enforcer.opa_eval = orig_scan, orig_opa
        con.close()

    # --- 5/6. deprecation coverage -----------------------------------------
    orig_active = rule_store.get_active_rules
    rule_store.get_active_rules = lambda language=None, con=None: []
    with Capture() as cap:
        assert triggers.on_deprecation("getActor", "2026-07-30", "typescript") is None
        assert "no enforcement rule" in cap.text(), cap.text()
        assert any(lv == "WARNING" for lv, _ in cap.records)
    ok("deprecation with no covering rule -> WARNING naming the gap")

    rule_store.get_active_rules = lambda language=None, con=None: [
        {"rule_id": "r-cov", "rule_text": "never call getActor directly",
         "yaml_pattern": ""}]
    with Capture() as cap:
        assert triggers.on_deprecation("getActor", "2026-07-30", "typescript") == "r-cov"
        assert "covered by rule r-cov" in cap.text(), cap.text()
        assert not any(lv == "WARNING" for lv, _ in cap.records), "must not warn when covered"
    ok("deprecation with a covering rule -> INFO, no warning")
    rule_store.get_active_rules = orig_active

    # a broken rule_store must not break sync's supersession path
    rule_store.get_active_rules = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down"))
    assert triggers.on_deprecation("getActor") is None
    rule_store.get_active_rules = orig_active
    ok("coverage check swallows a rule_store failure")

    # --- 7/8. connection manager -------------------------------------------
    tmp = isolate_db()
    c1, c2 = db.get_db(), db.get_db()
    assert c1 is c2, "get_db must reuse the thread's connection"
    assert Path(os.environ["CHRONOS_SQLITE"]).exists()
    ok("get_db returns the same connection twice within a thread")

    tmp2 = Path(tempfile.mkdtemp())
    os.environ["CHRONOS_SQLITE"] = str(tmp2 / "other.db")
    db.reset()
    c3 = db.get_db()
    assert c3 is not c1 and Path(tmp2 / "other.db").exists()
    ok("get_db honours CHRONOS_SQLITE (new path -> new database file)")

    # every wedge writes to that one file
    db.reset()
    tmp3 = isolate_db()
    con = ledger.connect()
    ledger.log_event(con, "n1", "a", "s", "touched", "r")
    rule_store.upsert_rule("r9", "python", "t", "y", {"passed": True}, con=con)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"intent_locks", "provenance_events", "enforcement_rules"} <= names, names
    con.close()
    assert len(list(tmp3.glob("*.db"))) == 1, list(tmp3.glob("*.db"))
    ok("locks, provenance and rules all live in the single chronos.db")

    # --- 9. legacy migration ------------------------------------------------
    tmp4 = Path(tempfile.mkdtemp())
    legacy = tmp4 / "ledger.db"
    old = sqlite3.connect(str(legacy))
    old.executescript(db.SCHEMA)
    old.execute("INSERT INTO intent_locks VALUES ('n-old','a','s','i','t','t2')")
    old.execute("INSERT INTO provenance_events (node_id,agent_id,session_id,action,reason,timestamp)"
                " VALUES ('n-old','a','s','touched','r','t')")
    old.execute("INSERT INTO enforcement_rules (rule_id,language,rule_text,yaml_pattern,"
                "status,detectability_passed,false_positive_risk,created_at)"
                " VALUES ('r-old','python','t','y','blocking',1,0,'t')")
    old.commit()
    old.close()

    os.environ["CHRONOS_SQLITE"] = str(tmp4 / "chronos.db")
    db.reset()
    db._migrated.clear()
    con = db.get_db()
    assert con.execute("SELECT count(*) c FROM intent_locks").fetchone()["c"] == 1
    assert con.execute("SELECT count(*) c FROM provenance_events").fetchone()["c"] == 1
    assert con.execute("SELECT count(*) c FROM enforcement_rules").fetchone()["c"] == 1
    assert con.execute("SELECT node_id FROM intent_locks").fetchone()["node_id"] == "n-old"
    assert not legacy.exists(), "legacy file should be renamed"
    assert (tmp4 / "ledger.db.bak").exists(), "legacy file must be kept as .bak"
    ok("legacy ledger.db migrated into chronos.db and renamed to .bak")

    con.close()
    db.reset()

    # --- 10. full regression -----------------------------------------------
    isolate_db()
    failures = []
    for suite in ("test_chronos", "test_wedge2", "test_wedge3", "test_wedge4"):
        r = subprocess.run([sys.executable, str(ROOT / "tests" / f"{suite}.py")],
                           capture_output=True, text=True, timeout=1800, cwd=ROOT)
        if r.returncode != 0 or "ALL PASS" not in r.stdout:
            failures.append(f"{suite} (exit {r.returncode})")
    assert not failures, f"pre-existing suites broke: {failures}"
    ok("full regression: all four wedge suites still pass after unification")

    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
