"""Self-check: the bi-temporal contract that the whole product rests on.

Run: python test_chronos.py
"""

import asyncio
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.pop("OPENAI_API_KEY", None)  # must never be needed

from chronos import query
from chronos.store import ensure_schema
from chronos.sync import Syncer, content_hash
from chronos.upstream import UpstreamGraph

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)   # before the refactor
T1 = datetime(2026, 6, 1, tzinfo=timezone.utc)   # the refactor
MID = datetime(2026, 3, 1, tzinfo=timezone.utc)  # between
LATER = datetime(2026, 9, 1, tzinfo=timezone.utc)

N = {
    "1": {"name": "handler", "path": "api.py", "kind": "Function"},
    "2": {"name": "old_api", "path": "legacy.py", "kind": "Function"},
    "3": {"name": "new_api", "path": "core.py", "kind": "Function"},
    "4": {"name": "router", "path": "web.py", "kind": "Function"},
}
BEFORE = [("1", "CALLS", "2"), ("4", "CALLS", "1")]
AFTER = [("1", "CALLS", "3"), ("4", "CALLS", "1")]


def fake_upstream(path, edges):
    """A stand-in for codebase-memory-mcp's sqlite graph, to exercise the adapter's
    runtime schema discovery without the binary installed."""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE IF NOT EXISTS nodes (id TEXT PRIMARY KEY, name TEXT, file_path TEXT, kind TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS edges (source_id TEXT, target_id TEXT, edge_type TEXT)")
    con.execute("DELETE FROM nodes"); con.execute("DELETE FROM edges")
    for i, n in N.items():
        con.execute("INSERT INTO nodes VALUES (?,?,?,?)", (i, n["name"], n["path"], n["kind"]))
    con.executemany("INSERT INTO edges VALUES (?,?,?)", [(s, d, t) for s, t, d in edges])
    con.commit(); con.close()


async def main():
    tmp = tempfile.mkdtemp()
    os.environ["CHRONOS_DB"] = os.path.join(tmp, "g.kz")
    from chronos.store import open_driver
    drv = open_driver()
    await ensure_schema(drv)
    G = "test"
    s = Syncer(drv, G)

    # --- adapter discovers an unknown schema by introspection ---
    up_path = os.path.join(tmp, "up.db")
    fake_upstream(up_path, BEFORE)
    up = UpstreamGraph(up_path)
    assert up.usable, up.schema_report()
    nodes, edges = up.nodes(), up.edges()
    up.close()
    assert len(nodes) == 4, nodes
    assert len(edges) == 2, edges
    print("ok  upstream adapter mapped an undocumented schema")

    # --- initial sync, no LLM key present ---
    st = await s.sync(nodes, edges, T0)
    assert st.edges_added == 2, st
    print(f"ok  initial sync with no LLM key ({st})")

    # --- idempotency: re-sync must not duplicate ---
    st2 = await s.sync(nodes, edges, T0)
    assert st2.edges_added == 0 and st2.edges_unchanged == 2, st2
    print(f"ok  re-sync is idempotent ({st2})")

    # --- the refactor: handler stops calling old_api, starts calling new_api ---
    fake_upstream(up_path, AFTER)
    up = UpstreamGraph(up_path)
    nodes2, edges2 = up.nodes(), up.edges()
    up.close()
    st3 = await s.sync(nodes2, edges2, T1)
    assert st3.edges_added == 1, st3
    assert st3.edges_invalidated == 1, st3
    print(f"ok  refactor superseded the old fact ({st3})")

    # --- THE CORE CLAIM: as-of queries return the right era ---
    before = await query.callees(drv, G, "handler", MID)
    names = [c["name"] for c in before["callees"]]
    assert names == ["old_api"], names
    print(f"ok  as-of {MID.date()} -> handler called {names}")

    now = await query.callees(drv, G, "handler", LATER)
    names_now = [c["name"] for c in now["callees"]]
    assert names_now == ["new_api"], names_now
    print(f"ok  as-of {LATER.date()} -> handler called {names_now}")

    # superseded history is still queryable, not deleted
    assert "old_api" not in names_now and "new_api" not in names

    # --- anti-hallucination: pre-history returns explicit no-data, not current state ---
    pre = await query.callees(drv, G, "handler", T0 - timedelta(days=365))
    assert pre["count"] == 0, pre
    assert "predates" in pre.get("no_data_reason", ""), pre
    print(f"ok  pre-history -> {pre['no_data_reason'][:58]}...")

    unknown = await query.callees(drv, G, "does_not_exist", LATER)
    assert unknown["count"] == 0 and "not present" in unknown["no_data_reason"]
    print("ok  unknown symbol -> explicit no-data, no silent fallback")

    # --- change feed ---
    ch = await query.changes(drv, G, T0 + timedelta(days=1), LATER)
    assert any(c["dst"] == "new_api" for c in ch["added"]), ch
    assert any(c["dst"] == "old_api" for c in ch["removed"]), ch
    print(f"ok  what-changed: +{len(ch['added'])} -{len(ch['removed'])}")

    # --- health ---
    h = await query.health(drv, G)
    assert h["facts_total"] == 3 and h["facts_current"] == 2, h
    print(f"ok  health: {h['facts_current']}/{h['facts_total']} facts current")

    # --- the UNWIND fast path must write rows graphiti itself can read back,
    #     otherwise we have silently forked the schema ---
    from graphiti_core.edges import EntityEdge
    from graphiti_core.nodes import EntityNode as EN
    live = await query._rows(drv, """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id=$g AND e.invalid_at IS NULL RETURN e.uuid AS u""", g=G)
    rt = await EntityEdge.get_by_uuid(drv, live[0]["u"])
    assert rt.valid_at is not None and rt.invalid_at is None and rt.group_id == G, rt
    assert rt.fact and rt.name, rt
    node_any = await query._rows(drv, "MATCH (n:Entity) WHERE n.group_id=$g RETURN n.uuid AS u", g=G)
    rn = await EN.get_by_uuid(drv, node_any[0]["u"])
    assert rn.name and "Entity" in rn.labels, rn
    assert rn.attributes.get("kind"), rn.attributes  # attributes survived JSON round-trip
    print("ok  fast-path rows read back through graphiti's own API")

    # --- qualified_name wins over path+name, which collides on real repos:
    #     two closures in one file, or a folder and file sharing a basename ---
    from chronos.sync import node_identity as ident
    a = {"name": "getAll", "path": "web/src/proxy.ts", "kind": "Function",
         "qname": "app.web.src.proxy.proxy.getAll"}
    b = {"name": "getAll", "path": "web/src/proxy.ts", "kind": "Function",
         "qname": "app.web.src.proxy.other.getAll"}
    assert ident(a) != ident(b), "same-named closures in one file must stay distinct"
    no_qn = {"name": "f", "path": "x.py", "kind": "Function", "qname": ""}
    assert ident(no_qn) == "x.py::f::Function", ident(no_qn)  # fallback intact
    print("ok  qualified_name identity disambiguates same-name symbols")

    # --- hash is stable and change-sensitive ---
    assert content_hash(nodes, edges) == content_hash(nodes, edges)
    assert content_hash(nodes, edges) != content_hash(nodes2, edges2)
    print("ok  content hash stable + change-sensitive")

    await drv.close()
    shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
