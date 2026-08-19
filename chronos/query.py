"""As-of queries over the bi-temporal graph (P0-4) and index health (P0-6)."""

from datetime import datetime, timezone

# A fact is visible at time t iff it had become true by t and had not yet been
# superseded. NULL invalid_at means "still true".
_WINDOW = "e.valid_at <= $t AND (e.invalid_at IS NULL OR e.invalid_at > $t)"

_CALLERS = f"""
MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
WHERE e.group_id = $g AND m.name = $name AND {_WINDOW}
RETURN n.name AS name, n.summary AS path, e.name AS rel
"""

_CALLEES = f"""
MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
WHERE e.group_id = $g AND n.name = $name AND {_WINDOW}
RETURN m.name AS name, m.summary AS path, e.name AS rel
"""

_EXISTS = "MATCH (n:Entity) WHERE n.group_id = $g AND n.name = $name RETURN n.name LIMIT 1"

_BOUNDS = """
MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
WHERE e.group_id = $g
RETURN min(e.valid_at) AS first, max(e.valid_at) AS last, count(e) AS total
"""


def _utc(t: datetime | None) -> datetime:
    t = t or datetime.now(timezone.utc)
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def _rows(driver, q, **kw):
    recs, _, _ = await driver.execute_query(q, **kw)
    return [dict(r) for r in recs]


async def callers(driver, group_id: str, name: str, at: datetime | None = None) -> dict:
    """Who called `name` as of `at`."""
    return await _directional(driver, _CALLERS, group_id, name, at, "callers")


async def callees(driver, group_id: str, name: str, at: datetime | None = None) -> dict:
    """What `name` called as of `at`."""
    return await _directional(driver, _CALLEES, group_id, name, at, "callees")


async def _directional(driver, q, group_id, name, at, key) -> dict:
    t = _utc(at)
    rows = await _rows(driver, q, g=group_id, name=name, t=t)
    out = {"as_of": t.isoformat(), "symbol": name, key: rows, "count": len(rows)}
    if rows:
        return out
    # Empty result is ambiguous: no data at that time, or no such symbol ever?
    # P0-4 / edge case: agents must get an explicit signal, never a silent fallback.
    #
    # The distinction below is load-bearing and must NOT be collapsed before it
    # reaches the API response. This function is the only place that still knows
    # WHY the list is empty; everything above it sees `count: 0` and cannot tell
    # "genuinely uncalled" from "the indexer never built the edge".
    known = await _rows(driver, _EXISTS, g=group_id, name=name)
    bounds = await _rows(driver, _BOUNDS, g=group_id)
    first = bounds[0].get("first") if bounds else None
    if not known:
        out["no_data_reason"] = "symbol_not_indexed"
        out["message"] = f"symbol '{name}' is not present in the graph for any time period"
    elif first and t < _utc(first):
        out["no_data_reason"] = "predates_earliest_record"
        out["message"] = (
            f"requested time {t.isoformat()} predates the graph's earliest record "
            f"({_utc(first).isoformat()}); no history exists for this period")
    else:
        # The symbol is indexed and the window is valid, so the graph holds no
        # edge. That is NOT the same as "the code has no callers" -- it was
        # exactly this message, phrased as a fact about the code, that led an
        # agent to conclude a method with three live call sites was unused.
        out["no_data_reason"] = "no_edges_in_graph"
        out["message"] = (
            f"'{name}' is indexed but the graph holds no {key} at {t.isoformat()}. "
            f"This means no {key[:-1]} EDGE was extracted -- not necessarily that "
            f"none exists in the source.")
    _annotate_coverage(out, group_id)
    return out


def _annotate_coverage(out: dict, group_id: str) -> None:
    """Attach index coverage to a zero result, so the caller can weigh it.

    A zero-caller answer from a graph with 8% call coverage means something
    very different from the same answer at 77%. Reading the manifest is cheap
    (one small JSON) and never fails the query: coverage is context, and a
    query must not break because context is unavailable.
    """
    try:
        from . import coverage as _cov
        from .upstream import find_db
        up = find_db()
        if not up:
            return
        m = _cov.read_manifest(up)
        if not m or m.get("group_id") != group_id:
            return
        c = m.get("callable_coverage")
        if c is None:
            return
        out["index_coverage"] = c
        if c < _cov.WARN_BELOW:
            out["coverage_warning"] = (
                f"only {c:.0%} of callable symbols in this index have any caller "
                f"edge, so this result may be incomplete.")
            if m.get("probable_cause"):
                out["coverage_warning"] += f" {m['probable_cause']}"
    except Exception:
        return  # context is optional; never fail a query for it


async def callers_diff(driver, group_id: str, name: str, since: datetime, until: datetime) -> dict:
    """Callers of `name` added/removed/stable between two points in time.

    Built as two as-of calls plus a client-side set difference, not a new
    graph query -- reuses the already-tested point-in-time primitive, and its
    no_data_reason, rather than risking a second way to be wrong about the
    same data.
    """
    t1, t2 = await callers(driver, group_id, name, since), await callers(driver, group_id, name, until)
    before = {c["name"]: c for c in t1["callers"]}
    after = {c["name"]: c for c in t2["callers"]}
    added = [after[n] for n in after.keys() - before.keys()]
    removed = [before[n] for n in before.keys() - after.keys()]
    stable = [after[n] for n in after.keys() & before.keys()]
    out = {
        "symbol": name, "since": t1["as_of"], "until": t2["as_of"],
        "added": added, "removed": removed, "stable": stable,
        "summary": {"added_count": len(added), "removed_count": len(removed),
                    "stable_count": len(stable)},
    }
    for key, res in (("since_no_data_reason", t1), ("until_no_data_reason", t2)):
        if "no_data_reason" in res:
            out[key] = res["no_data_reason"]
    return out


async def changes(driver, group_id: str, since: datetime, until: datetime | None = None) -> dict:
    """Structural facts created or superseded in a window -- 'what changed here'."""
    s, u = _utc(since), _utc(until)
    added = await _rows(driver, """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id=$g AND e.valid_at > $s AND e.valid_at <= $u
        RETURN n.name AS src, e.name AS rel, m.name AS dst""", g=group_id, s=s, u=u)
    removed = await _rows(driver, """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id=$g AND e.invalid_at > $s AND e.invalid_at <= $u
        RETURN n.name AS src, e.name AS rel, m.name AS dst""", g=group_id, s=s, u=u)
    out = {"since": s.isoformat(), "until": u.isoformat(),
           "added": added, "removed": removed,
           "count": len(added) + len(removed)}
    if out["count"] == 0:
        out["no_data_reason"] = "no_changes_in_window"
    return out


# A node is orphaned only if it once had facts and every one is now superseded.
# A node with NO facts at all is NOT an orphan -- we sync every upstream symbol but
# only edges of TEMPORAL_EDGE_TYPES, so most nodes legitimately have no edges and
# deleting them would destroy valid symbols (16,726 of them on one real repo).
_ORPHAN_MATCH = """
MATCH (x:Entity) WHERE x.group_id = $g
  AND EXISTS { MATCH (x)-[:RELATES_TO]->(a:RelatesToNode_) WHERE a.invalid_at IS NOT NULL }
  AND NOT EXISTS { MATCH (x)-[:RELATES_TO]->(b:RelatesToNode_) WHERE b.invalid_at IS NULL }
  AND NOT EXISTS { MATCH (c:RelatesToNode_)-[:RELATES_TO]->(x) WHERE c.invalid_at IS NULL }
"""


async def orphans(driver, group_id: str, sample: int = 10) -> dict:
    """Nodes whose every fact has been superseded -- left behind by identity
    migrations. Reports counts plus a sample so a dry run is reviewable."""
    total = await _rows(driver, "MATCH (x:Entity) WHERE x.group_id=$g RETURN count(x) AS c",
                        g=group_id)
    n = await _rows(driver, _ORPHAN_MATCH + "RETURN count(x) AS c", g=group_id)
    rows = await _rows(driver, _ORPHAN_MATCH + "RETURN x.name AS name, x.summary AS path LIMIT $k",
                       g=group_id, k=max(0, int(sample)))
    tot = total[0]["c"] if total else 0
    cnt = n[0]["c"] if n else 0
    return {"group_id": group_id, "nodes_total": tot, "orphans": cnt,
            "pct": round(100.0 * cnt / tot, 1) if tot else 0.0,
            "sample": [dict(r) for r in rows]}


async def collect_orphans(driver, group_id: str) -> dict:
    """Delete orphaned nodes and the superseded facts attached to them.

    Destructive; callers gate this behind an explicit flag. History for nodes that
    still matter is untouched -- only nodes with no current fact in either
    direction are removed.
    """
    before = await orphans(driver, group_id)
    if before["orphans"]:
        # Resolve the victims up front. Deleting their fact nodes first would stop
        # them matching _ORPHAN_MATCH (it requires a superseded fact), leaving the
        # entities behind -- so capture uuids, then delete facts, then entities.
        victims = [r["u"] for r in await _rows(driver, _ORPHAN_MATCH + "RETURN x.uuid AS u",
                                               g=group_id)]
        for i in range(0, len(victims), 1000):
            chunk = victims[i:i + 1000]
            await driver.execute_query("""
                MATCH (x:Entity)-[:RELATES_TO]->(e:RelatesToNode_)
                WHERE list_contains($us, x.uuid) DETACH DELETE e""", us=chunk)
            await driver.execute_query("""
                MATCH (e:RelatesToNode_)-[:RELATES_TO]->(x:Entity)
                WHERE list_contains($us, x.uuid) DETACH DELETE e""", us=chunk)
            await driver.execute_query(
                "MATCH (x:Entity) WHERE list_contains($us, x.uuid) DETACH DELETE x", us=chunk)
    after = await _rows(driver, "MATCH (x:Entity) WHERE x.group_id=$g RETURN count(x) AS c",
                        g=group_id)
    return {"group_id": group_id, "deleted": before["orphans"],
            "nodes_before": before["nodes_total"],
            "nodes_after": after[0]["c"] if after else 0}


async def health(driver, group_id: str, upstream_path=None) -> dict:
    """P0-6: answer 'is this graph safe to rely on right now' at a glance."""
    b = await _rows(driver, _BOUNDS, g=group_id)
    b = b[0] if b else {}
    nodes = await _rows(driver, "MATCH (n:Entity) WHERE n.group_id=$g RETURN count(n) AS c", g=group_id)
    live = await _rows(driver, """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id=$g AND e.invalid_at IS NULL RETURN count(e) AS c""", g=group_id)
    last = b.get("last")
    age_min = None
    if last:
        age_min = round((datetime.now(timezone.utc) - _utc(last)).total_seconds() / 60, 1)
    total = b.get("total") or 0
    status = "empty" if not total else ("stale" if (age_min or 0) > 60 else "fresh")
    out = {
        "group_id": group_id,
        "status": status,
        "nodes": nodes[0]["c"] if nodes else 0,
        "facts_total": total,
        "facts_current": live[0]["c"] if live else 0,
        "earliest_record": _utc(b["first"]).isoformat() if b.get("first") else None,
        "last_sync": _utc(last).isoformat() if last else None,
        "minutes_since_sync": age_min,
    }
    if upstream_path:
        out["upstream_db"] = str(upstream_path)
        # Parse coverage comes from upstream's own reporting -- a graph that is
        # fresh but only parsed 60% of files is not safe to rely on, and P0-6 is
        # about answering exactly that question.
        try:
            from .upstream import UpstreamGraph
            g = UpstreamGraph(upstream_path)
            cov = g.coverage()
            g.close()
            if cov:
                out["coverage"] = cov
                # Upstream reports problem files, not a parsed/total ratio, so we
                # flag their presence rather than inventing a percentage.
                if cov.get("files_with_issues") and out["status"] == "fresh":
                    out["status"] = "fresh-partial"
        except Exception as e:  # coverage is advisory; never fail health on it
            out["coverage_error"] = str(e)
    return out
