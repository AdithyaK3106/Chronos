"""Chronos MCP server -- the unified surface agents talk to (P0-4).

Tool set is deliberately small (open question in the PRD: mirror upstream's 14-15
tools 1:1, or ship an opinionated set?). We ship the temporal tools that only
Chronos can answer, and leave current-state structural search to the upstream
codebase-memory-mcp server, which the agent can run alongside this one.
ponytail: proxying 15 upstream tools we'd add nothing to is pure surface area.
"""

import os
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from . import query
from .store import open_driver

GROUP = os.environ.get("CHRONOS_GROUP_ID", "default")
mcp = FastMCP("chronos")

_driver = None


async def driver():
    global _driver
    if _driver is None:
        _driver = open_driver()
    return _driver


def _parse(when: str | None) -> datetime | None:
    """Accept ISO timestamps, 'now', or relative like '7d' / '12h'."""
    if not when or when == "now":
        return None
    w = when.strip()
    if w and w[-1] in "dh" and w[:-1].replace(".", "").isdigit():
        n = float(w[:-1])
        delta = timedelta(days=n) if w[-1] == "d" else timedelta(hours=n)
        return datetime.now(timezone.utc) - delta
    try:
        d = datetime.fromisoformat(w.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(f"could not parse time '{when}'; use ISO-8601, 'now', '7d', or '12h'") from e


@mcp.tool()
async def as_of_callers(symbol: str, when: str = "now") -> dict:
    """Who called `symbol` at a point in time.

    when: ISO-8601 timestamp, 'now', or relative ('7d', '12h').
    Returns an explicit no_data_reason instead of falling back to current state.
    """
    return await query.callers(await driver(), GROUP, symbol, _parse(when))


@mcp.tool()
async def as_of_callees(symbol: str, when: str = "now") -> dict:
    """What `symbol` called at a point in time. Use to check whether a
    function used a deprecated API before a refactor."""
    return await query.callees(await driver(), GROUP, symbol, _parse(when))


@mcp.tool()
async def as_of_impact(symbol: str, when: str = "now", depth: int = 2) -> dict:
    """Transitive callers of `symbol` at a point in time -- blast radius of a change."""
    d, t = await driver(), _parse(when)
    seen, frontier, layers = {symbol}, [symbol], []
    for _ in range(max(1, min(depth, 5))):
        nxt = []
        for s in frontier:
            r = await query.callers(d, GROUP, s, t)
            for c in r["callers"]:
                if c["name"] not in seen:
                    seen.add(c["name"])
                    nxt.append(c["name"])
        if not nxt:
            break
        layers.append(nxt)
        frontier = nxt
    return {"symbol": symbol, "as_of": (t or datetime.now(timezone.utc)).isoformat(),
            "layers": layers, "total_impacted": len(seen) - 1}


@mcp.tool()
async def what_changed(since: str = "7d", until: str = "now") -> dict:
    """Structural facts added or superseded in a time window."""
    s = _parse(since) or datetime.now(timezone.utc) - timedelta(days=7)
    return await query.changes(await driver(), GROUP, s, _parse(until))


@mcp.tool()
async def index_health() -> dict:
    """Freshness, size, and staleness of the graph. Check before trusting results."""
    return await query.health(await driver(), GROUP)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
