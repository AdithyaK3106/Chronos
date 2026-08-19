"""Chronos MCP server -- the unified surface agents talk to (P0-4).

Tool set is deliberately small (open question in the PRD: mirror upstream's 14-15
tools 1:1, or ship an opinionated set?). We ship the temporal tools that only
Chronos can answer, and leave current-state structural search to the upstream
codebase-memory-mcp server, which the agent can run alongside this one.
ponytail: proxying 15 upstream tools we'd add nothing to is pure surface area.
"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP

from . import groups, query
from .store import GraphLocked, ensure_schema, open_driver

GROUP = groups.resolve(os.environ.get("CHRONOS_GROUP_ID"),
                        os.environ.get("CHRONOS_REPO_PATH"))
mcp = FastMCP("chronos")

_driver = None
_schema_ready = False
# Serialises opening. Concurrent tool calls must not each try to open the store.
_driver_lock = asyncio.Lock()

# Opening measures ~2-3s uncontended. A bounded wait is what turns "the server
# is dead" into "the graph is busy, here is why".
#
# 30s was not enough in practice: this window also has to absorb retries
# against the trace_processor race (see driver()'s docstring) and that race's
# actual duration was measured to sometimes exceed 30s, not the milliseconds
# assumed when the retry was added -- a real GraphLocked surfaced after the
# full 30s, then the very next call succeeded instantly. 90s keeps the same
# "genuinely stuck" cutoff meaningful (a real cross-process holder still
# fails, just after longer) while giving the self-resolving case real room.
DRIVER_OPEN_TIMEOUT = float(os.environ.get("CHRONOS_DRIVER_TIMEOUT", "90"))

# MEASURED, and it constrains the whole design: `await driver.close()` does NOT
# release Kuzu's lock. Verified 2026-08-17 -- open, close, then reopen in the
# same process raises GraphLocked, and still does after `del` + gc.collect().
# The lock is held until the PROCESS EXITS.
#
# So an idle-release scheme (close the driver, reopen on the next query) cannot
# work: the reopen always fails and the server is permanently broken from that
# point. It was implemented, tested against a fake driver that *did* release,
# passed, and would have shipped a server that dies after its first idle
# period. Only a real reopen caught it.
#
# The consequence has to be stated rather than engineered around: one process
# owns the graph for its lifetime. Everything below follows from that.


async def driver():
    """The one Kuzu driver for this process. Opened once, held until exit.

    THE BUG THIS FIXES
    ------------------
    The first graph-backed tool call in a fresh MCP server used to return
    nothing at all -- measured at 75s, 90s, 200s, 420s and 500s+, at ~0% CPU,
    with no error. Every MCP client times out long before that and reports the
    server as dead, which is exactly what a pilot agent concluded.

    Cause: Kuzu allows ONE holder of the store per process, and open_driver()
    WAITS rather than failing when the store is busy. Three separate things
    then competed for it -- wedges 1, 2 and 4 each kept their own driver
    global, so a client using tools from two wedges deadlocked the server
    against itself, and a background trace-drain thread raced them both.

    Three properties fix it, and each has a test in tests/test_mcp_driver.py:

    1. SINGLE OWNER. Wedges 2 and 4 delegate here. One driver per process is
       not an optimisation, it is the only correct number.
    2. OFF THE EVENT LOOP. open_driver() is synchronous and takes ~2-3s;
       running it inline stalls the stdio transport that serves MCP frames.
    3. BOUNDED WAIT. A timeout converts an indefinite hang into a fast,
       explicit, actionable error naming the likely holder.

    NOT DONE, deliberately: releasing the graph when idle. close() does not
    release Kuzu's lock (see the note above the constants), so a release-and-
    reopen scheme leaves the server permanently unable to reopen. A long-lived
    server therefore does hold the graph, and `chronos index` must stop it
    first -- surfaced in the error message rather than hidden.
    """
    global _driver, _schema_ready
    async with _driver_lock:
        if _driver is None:
            # trace_processor._resolve_touched opens its OWN driver on a
            # background thread (deliberately -- it must not pin this driver
            # across an LLM call). Kuzu's lock is a real OS file lock, unaware
            # both opens share a process, so this module's very first open can
            # lose that race to a same-process caller that will release within
            # milliseconds. Retrying inside DRIVER_OPEN_TIMEOUT turns a
            # self-resolving race into a few hundred ms of extra latency
            # instead of a raw GraphLocked surfacing to whatever tool call
            # happened to go first.
            #
            # Kuzu's own lock handling is inconsistent, confirmed 2026-08-18:
            # sometimes open_driver() raises GraphLocked immediately (the case
            # above retries), other times it blocks synchronously inside
            # Kuzu's C layer with no exception at all -- the ONLY thing that
            # can interrupt that is asyncio.wait_for's timeout, which raises
            # TimeoutError, not GraphLocked. When that happens the thread
            # running open_driver() is still blocked inside Kuzu and Python
            # cannot cancel it -- it leaks for the rest of the process
            # lifetime. So on TimeoutError this stops immediately rather than
            # retrying: a second attempt would risk leaking a second thread on
            # top of the first for no benefit, since the underlying cause
            # (Kuzu blocking instead of failing fast) is not something a retry
            # here can fix.
            deadline = time.monotonic() + DRIVER_OPEN_TIMEOUT
            last_err = None
            timed_out = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    _driver = await asyncio.wait_for(
                        asyncio.to_thread(open_driver), timeout=remaining)
                    last_err = None
                    break
                except GraphLocked as e:
                    last_err = e
                    await asyncio.sleep(0.2)
                except asyncio.TimeoutError:
                    timed_out = True
                    break
            if _driver is None:
                if timed_out:
                    raise RuntimeError(
                        f"could not open the graph within {DRIVER_OPEN_TIMEOUT}s "
                        f"-- Kuzu appears to be blocking on the lock rather than "
                        f"failing fast (a background thread is now leaked; this "
                        f"is a known Kuzu behavior, not recoverable by retrying). "
                        f"{'Last known holder: ' + str(last_err) if last_err else ''} "
                        f"Check with `python -m chronos daemon status` and look "
                        f"for stray chronos-mcp processes; a server restart "
                        f"clears the leaked thread."
                    ) from None
                if last_err is not None:
                    raise last_err
                raise RuntimeError(
                    f"could not open the graph within {DRIVER_OPEN_TIMEOUT}s. "
                    f"Kuzu allows one process at a time and another one is "
                    f"holding it -- typically another chronos-mcp server, a "
                    f"`chronos index`/`sync` run, or `chronos daemon`. "
                    f"Check with `python -m chronos daemon status` and look "
                    f"for stray chronos-mcp processes."
                ) from None
            _schema_ready = False
        if not _schema_ready:
            await ensure_schema(_driver)
            _schema_ready = True
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
async def as_of_diff(symbol: str, since: str, until: str) -> dict:
    """Who started/stopped calling `symbol` between two points in time.

    Two as_of_callers calls joined client-side, not a new graph query. Each
    side's no_data_reason is propagated rather than collapsed into a silent
    empty set -- an agent must know "no callers were removed" apart from
    "the symbol wasn't indexed at since".
    """
    return await query.callers_diff(await driver(), GROUP, symbol, _parse(since), _parse(until))


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
    root = None
    for hop in range(max(1, min(depth, 5))):
        nxt = []
        for s in frontier:
            r = await query.callers(d, GROUP, s, t)
            if hop == 0:
                root = r  # the seed query carries the why-empty diagnosis
            for c in r["callers"]:
                if c["name"] not in seen:
                    seen.add(c["name"])
                    nxt.append(c["name"])
        if not nxt:
            break
        layers.append(nxt)
        frontier = nxt
    out = {"symbol": symbol, "as_of": (t or datetime.now(timezone.utc)).isoformat(),
           "layers": layers, "total_impacted": len(seen) - 1}
    # A bare {"layers": [], "total_impacted": 0} is indistinguishable from
    # "not indexed", "no callers", and "wrong symbol name". The seed query
    # already knows which; carry it rather than dropping it on the floor.
    if not layers and root:
        for k in ("no_data_reason", "message", "index_coverage", "coverage_warning"):
            if k in root:
                out[k] = root[k]
    return out


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
