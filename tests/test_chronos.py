"""Self-check: the bi-temporal contract that the whole product rests on.

Run: python tests/test_chronos.py
"""

import asyncio
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    # no_data_reason is a machine-readable code and `message` carries the prose.
    # Both are asserted: an agent branches on the code, a human reads the message,
    # and a regression in either one is a P0-4 violation.
    pre = await query.callees(drv, G, "handler", T0 - timedelta(days=365))
    assert pre["count"] == 0, pre
    assert pre.get("no_data_reason") == "predates_earliest_record", pre
    assert "predates" in pre.get("message", ""), pre
    print(f"ok  pre-history -> {pre['no_data_reason']}")

    unknown = await query.callees(drv, G, "does_not_exist", LATER)
    assert unknown["count"] == 0
    assert unknown["no_data_reason"] == "symbol_not_indexed", unknown
    assert "not present" in unknown.get("message", ""), unknown
    print("ok  unknown symbol -> explicit no-data, no silent fallback")

    # The distinction that matters: a symbol that IS indexed but has no edge
    # must NOT report the same reason as one that was never indexed. Collapsing
    # these is what told an agent a method with 3 live callers was unused.
    indexed_no_edge = await query.callers(drv, G, "handler", LATER)
    if indexed_no_edge["count"] == 0:
        assert indexed_no_edge["no_data_reason"] == "no_edges_in_graph", indexed_no_edge
        assert "not necessarily" in indexed_no_edge.get("message", ""), indexed_no_edge
        print("ok  indexed-but-no-edge -> distinct from not-indexed")

    # --- change feed ---
    ch = await query.changes(drv, G, T0 + timedelta(days=1), LATER)
    assert any(c["dst"] == "new_api" for c in ch["added"]), ch
    assert any(c["dst"] == "old_api" for c in ch["removed"]), ch
    print(f"ok  what-changed: +{len(ch['added'])} -{len(ch['removed'])}")

    # --- P0-4 regression: a narrow window must never return MORE than a wide
    #     one over the same range. The bug this pins returned every row
    #     regardless of since/until -- a 7-day window outnumbered a 90-day one. ---
    now = datetime.now(timezone.utc)
    narrow = await query.changes(drv, G, now - timedelta(days=7), now)
    wide = await query.changes(drv, G, now - timedelta(days=90), now)
    assert narrow["count"] <= wide["count"], (narrow, wide)
    assert narrow.get("no_data_reason") == "no_changes_in_window", narrow
    print(f"ok  narrow window ({narrow['count']}) <= wide window ({wide['count']})")

    # --- as_of_diff: symbol-scoped diff over two as_of_callers calls. This is
    #     the litestar ResponseCacheMiddleware shape -- router called handler
    #     before the refactor, still does after; handler's OWN callees moved
    #     from old_api to new_api, so diff old_api's callers across the window. ---
    diff = await query.callers_diff(drv, G, "old_api", MID, LATER)
    assert [c["name"] for c in diff["removed"]] == ["handler"], diff
    assert diff["added"] == [], diff
    assert diff["summary"] == {"added_count": 0, "removed_count": 1, "stable_count": 0}, diff
    print("ok  as_of_diff: old_api lost its only caller across the refactor")

    stable_diff = await query.callers_diff(drv, G, "new_api", T1, LATER)
    assert [c["name"] for c in stable_diff["stable"]] == ["handler"], stable_diff
    print("ok  as_of_diff: new_api's caller is stable once introduced")

    # --- health ---
    h = await query.health(drv, G)
    assert h["facts_total"] == 3 and h["facts_current"] == 2, h
    print(f"ok  health: {h['facts_current']}/{h['facts_total']} facts current")

    # --- parse coverage from upstream's own reporting (P0-1): a partial index
    #     must never be presented as a clean one ---
    con = sqlite3.connect(up_path)
    con.execute("CREATE TABLE index_coverage (project TEXT, rel_path TEXT, kind TEXT, detail TEXT)")
    con.execute("CREATE TABLE index_coverage_meta (project TEXT, index_mode TEXT, "
                "recording_status TEXT, ignored_files_total INT)")
    con.execute("INSERT INTO index_coverage VALUES ('p','a.py','parse_partial','1-2')")
    con.execute("INSERT INTO index_coverage VALUES ('p','b.css','parse_partial','5-5')")
    con.execute("INSERT INTO index_coverage_meta VALUES ('p','fast','complete',3)")
    con.commit(); con.close()
    cov = UpstreamGraph(up_path).coverage()
    assert cov["files_with_issues"] == 2, cov
    assert cov["issues_by_kind"]["parse_partial"] == 2, cov
    hc = await query.health(drv, G, up_path)
    assert hc["coverage"]["files_with_issues"] == 2, hc
    # status is 'stale' here because the fixture's facts are backdated; the
    # partial-flag only downgrades an otherwise-fresh graph.
    assert hc["status"] in ("stale", "fresh-partial"), hc["status"]
    print(f"ok  parse coverage surfaced ({hc['coverage']['issues_by_kind']}) -> status={hc['status']}")

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

    # --- the indexer path must carry qname through to identity, or every node it
    #     writes silently downgrades to the colliding path+name scheme ---
    import chronos.indexer as indexer
    fake_rows = [
        {"kind": "node", "id": "1", "name": "getAll", "path": "p.ts",
         "node_kind": "Function", "qname": "app.p.outer.getAll"},
        {"kind": "node", "id": "2", "name": "getAll", "path": "p.ts",
         "node_kind": "Function", "qname": "app.p.other.getAll"},
        {"kind": "edge", "src": "1", "type": "CALLS", "dst": "2"},
    ]
    orig = indexer.index_repo
    indexer.index_repo = lambda *a, **k: fake_rows
    try:
        gn, ge = indexer.index_repo_graph("ignored")
    finally:
        indexer.index_repo = orig
    assert all(v.get("qname") for v in gn.values()), f"indexer dropped qname: {gn}"
    assert ident(gn["1"]) != ident(gn["2"]), "indexer path must not collapse distinct symbols"
    assert ge == [("1", "CALLS", "2")], ge
    print("ok  indexer path preserves qualified_name identity")

    # --- gc removes only nodes whose facts are ALL superseded, and never touches
    #     a live fact. A node with no facts at all is a valid symbol, not an
    #     orphan: we sync every upstream node but only TEMPORAL_EDGE_TYPES edges. ---
    gnodes = {k: {"name": k, "path": "g.py", "kind": "Function", "qname": f"gc.{k}"}
              for k in ("keep_a", "keep_b", "dies", "lonely")}
    gs = Syncer(drv, "gc")
    await gs.sync(gnodes, [("keep_a", "CALLS", "keep_b"), ("dies", "CALLS", "keep_b")], T0)
    await gs.sync(gnodes, [("keep_a", "CALLS", "keep_b")], T1)  # dies-> is superseded
    pre = await query.health(drv, "gc")
    o = await query.orphans(drv, "gc", sample=5)
    assert o["orphans"] == 1, f"only 'dies' is orphaned: {o}"
    assert o["sample"][0]["name"] == "dies", o["sample"]
    g_res = await query.collect_orphans(drv, "gc")
    post = await query.health(drv, "gc")
    assert g_res["deleted"] == 1, g_res
    assert post["facts_current"] == pre["facts_current"], "gc destroyed a live fact"
    assert (await query.callers(drv, "gc", "keep_b"))["count"] == 1, "live edge lost"
    assert (await query.orphans(drv, "gc", sample=0))["orphans"] == 0
    # 'lonely' has no facts at all and must survive
    assert await query._rows(drv, "MATCH (x:Entity) WHERE x.group_id='gc' AND x.name='lonely' "
                                  "RETURN 1 AS o"), "gc deleted a symbol that never had facts"
    print(f"ok  gc: deleted {g_res['deleted']} orphan, kept live facts and fact-less symbols")

    # --- hash is stable and change-sensitive ---
    assert content_hash(nodes, edges) == content_hash(nodes, edges)
    assert content_hash(nodes, edges) != content_hash(nodes2, edges2)
    print("ok  content hash stable + change-sensitive")

    await drv.close()
    shutil.rmtree(tmp, ignore_errors=True)


def language_tagging():
    """Nodes must carry a language, derived from the file extension.

    Wedge 4 filters active rules by language; before this the field did not
    exist, so every rule applied to every file regardless of its `language:`.
    Pure-function checks plus a real index over a mixed-language fixture.
    """
    from chronos import indexer

    cases = {"a.py": "python", "b.ts": "typescript", "c.tsx": "typescript",
             "d.js": "javascript", "e.go": "go", "f.rs": "rust", "g.java": "java",
             "h.rb": "ruby", "i.c": "c", "j.cpp": "cpp", "k.h": "c",
             "l.cs": "csharp", "m.kt": "kotlin", "n.swift": "swift",
             "src/deep/o.PY": "python",            # case-insensitive
             "p.txt": "unknown", "noext": "unknown", "": "unknown"}
    for path, want in cases.items():
        got = indexer.node_language(path)
        assert got == want, f"{path!r} -> {got!r}, expected {want!r}"
    assert indexer.node_language(None) == "unknown", "None path must not raise"
    print(f"ok  language derived from extension for {len(cases)} paths "
          "(unknown, never None)")

    if indexer.binary_path() is None:
        print(f"skip language index check: indexer not built ({indexer.BUILD_CMD})")
        return

    tmp = tempfile.mkdtemp()
    Path(tmp, "mod.py").write_text("def py_fn():\n    return 1\n", encoding="utf-8")
    Path(tmp, "mod.ts").write_text("export function tsFn() { return 1; }\n", encoding="utf-8")
    nodes, _ = indexer.index_repo_graph(tmp)
    assert nodes, "fixture produced no nodes"

    assert all(n.get("language") is not None for n in nodes.values()), \
        "every node must have a language (unknown is fine, None is not)"
    langs = {n["language"] for n in nodes.values()}
    assert "python" in langs, f"no python node from mod.py; got {langs}"
    assert "typescript" in langs, f"no typescript node from mod.ts; got {langs}"
    py = [n for n in nodes.values() if n["path"].endswith(".py")]
    ts = [n for n in nodes.values() if n["path"].endswith(".ts")]
    assert py and all(n["language"] == "python" for n in py), py[:2]
    assert ts and all(n["language"] == "typescript" for n in ts), ts[:2]
    print(f"ok  real index tagged {len(py)} .py nodes python and {len(ts)} .ts nodes "
          f"typescript ({len(nodes)} nodes, languages={sorted(langs)})")
    shutil.rmtree(tmp, ignore_errors=True)


async def roundtrip(repo: str | None = None):
    """Every symbol the indexer writes must be findable through the query path.

    This is the one test that crosses both input paths -- indexer writes, graph
    reads -- which is exactly the seam the qualified_name bug lived in: the two
    sides disagreed on identity and nothing failed. Skipped (not failed) when the
    vendored indexer isn't built, since it needs a real index to be meaningful.
    """
    from chronos import indexer

    if indexer.binary_path() is None:
        print("skip roundtrip: vendored indexer not built "
              f"({indexer.BUILD_CMD})")
        return
    repo = repo or str(Path(__file__).parent)
    tmp = tempfile.mkdtemp()
    os.environ["CHRONOS_DB"] = os.path.join(tmp, "rt.kz")
    G = "roundtrip"

    nodes, edges = indexer.index_repo_graph(repo)
    assert nodes, f"indexer returned no nodes for {repo}"

    from chronos.store import open_driver
    drv = open_driver()
    await ensure_schema(drv)
    await Syncer(drv, G).sync(nodes, edges)

    # Ask for each symbol the same way as_of_callers does: by derived uuid.
    from chronos.sync import _uuid
    from chronos.sync import node_identity as ident
    missing = []
    for n in nodes.values():
        u = _uuid(G, "node", ident(n))
        if not await query._rows(drv, "MATCH (x:Entity) WHERE x.uuid=$u RETURN 1 AS o", u=u):
            missing.append(n.get("qname") or f"{n['path']}::{n['name']}")

    found = len(nodes) - len(missing)
    if missing:
        print(f"FAIL roundtrip: {found}/{len(nodes)} found, {len(missing)} missing")
        for q in missing[:5]:
            print(f"    missing: {q}")
    assert not missing, f"{len(missing)} of {len(nodes)} nodes did not round-trip"
    print(f"ok  round-trip: {found}/{len(nodes)} indexed nodes found via the query path")

    await drv.close()
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    # ALL PASS prints only after every check, including the ones that run
    # outside main() -- otherwise a failure here appears below a pass banner.
    asyncio.run(main())
    language_tagging()
    asyncio.run(roundtrip())
    print(chr(10) + "ALL PASS")
