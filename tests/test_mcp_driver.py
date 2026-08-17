"""The MCP driver must never hang.

Run: pytest tests/test_mcp_driver.py -v

WHAT THESE PIN
--------------
Kuzu allows exactly one holder of the store per process. The MCP server used to
open the graph on first use and never release it, and `open_driver()` waits
rather than failing when the store is busy. Together those produced the worst
failure mode this project has had: the first graph-backed tool call in a fresh
server returned nothing at all -- measured at 75s, 90s, 200s, 420s and 500s+,
at ~0% CPU, with no error. Every MCP client reads that as a dead server.

Defects that fed it, each with a test here:
  1. no bound on how long opening may take        -> test_open_is_bounded
  2. opening ran on the event loop, stalling stdio -> test_open_runs_off_the_event_loop
  3. wedges 2 and 4 each opened their own driver   -> test_single_driver_owner
  4. concurrent calls raced to open                -> test_concurrent_calls_open_once

And one constraint discovered the hard way: close() does NOT release Kuzu's
lock, so the driver must be held for the process lifetime
  -> test_driver_is_held_not_released
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import wedge1_mcp, wedge2_mcp, wedge4_mcp  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    """Each test starts with no driver."""
    wedge1_mcp._driver = None
    wedge1_mcp._schema_ready = False
    yield
    wedge1_mcp._driver = None
    wedge1_mcp._schema_ready = False


def test_single_driver_owner():
    """Wedges 2 and 4 must delegate, not keep their own driver.

    Three drivers in one process meant that using a Wedge 1 tool and then a
    Wedge 4 tool deadlocked the server against itself.
    """
    assert not hasattr(wedge2_mcp, "_driver"), "wedge2 kept its own driver global"
    assert not hasattr(wedge4_mcp, "_driver"), "wedge4 kept its own driver global"
    src2 = wedge2_mcp.driver.__doc__ or ""
    src4 = wedge4_mcp.driver.__doc__ or ""
    assert "wedge1" in src2.lower() and "wedge1" in src4.lower()


def test_open_is_bounded(monkeypatch):
    """A graph that never opens must raise, not hang.

    This is the regression that matters: open_driver() blocks indefinitely when
    another process holds the store, so without a timeout the tool call never
    returns and the client cannot tell a busy graph from a crashed server.
    """
    def never_opens():
        time.sleep(30)  # far longer than the timeout under test

    monkeypatch.setattr(wedge1_mcp, "open_driver", never_opens)
    monkeypatch.setattr(wedge1_mcp, "DRIVER_OPEN_TIMEOUT", 0.5)

    async def go():
        t0 = time.monotonic()
        with pytest.raises(RuntimeError) as ei:
            await wedge1_mcp.driver()
        return time.monotonic() - t0, str(ei.value)

    elapsed, msg = asyncio.run(go())
    assert elapsed < 5, f"took {elapsed:.1f}s -- the wait is not bounded"
    # The message must name the cause and a next step, not just fail.
    assert "one process at a time" in msg
    assert "chronos-mcp" in msg or "daemon status" in msg


def test_open_runs_off_the_event_loop(monkeypatch):
    """Opening must not block the loop that serves MCP frames.

    open_driver() is synchronous and takes seconds. Run inline it stalls the
    stdio transport, so the server stops answering while it works.
    """
    opened_on = {}

    def slow_open():
        import threading
        opened_on["thread"] = threading.current_thread().name
        time.sleep(0.3)
        return _FakeDriver()

    monkeypatch.setattr(wedge1_mcp, "open_driver", slow_open)

    async def go():
        main = asyncio.current_task().get_name()
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        hb = asyncio.create_task(heartbeat())
        await wedge1_mcp.driver()
        hb.cancel()
        return ticks, main

    ticks, _ = asyncio.run(go())
    # If opening blocked the loop, the heartbeat could not have run.
    assert ticks >= 2, f"event loop was blocked during open (only {ticks} ticks)"


def test_driver_is_held_not_released(monkeypatch):
    """The driver must be held for the process lifetime, never auto-closed.

    This pins a constraint of the storage engine, not a preference. MEASURED:
    `await driver.close()` does NOT release Kuzu's lock -- reopening in the
    same process raises GraphLocked, even after del + gc.collect(). So an
    idle-release scheme (close when quiet, reopen on demand) leaves the server
    permanently unable to reopen.

    That scheme was written, tested against a fake driver that DID release,
    and passed -- it would have shipped a server that breaks after its first
    idle period. This test exists so nobody adds it back.
    """
    monkeypatch.setattr(wedge1_mcp, "open_driver", lambda: _FakeDriver())

    async def go():
        d = await wedge1_mcp.driver()
        await asyncio.sleep(0.4)
        return d, await wedge1_mcp.driver()

    first, second = asyncio.run(go())
    assert not first.closed, "driver was closed; it cannot be reopened after that"
    assert first is second, "driver was replaced -- a reopen would raise GraphLocked"
    assert not hasattr(wedge1_mcp, "_release_when_idle"), (
        "idle release reintroduced; close() does not free the Kuzu lock")


def test_reuse_while_active(monkeypatch):
    """Back-to-back calls reuse one driver rather than reopening per query."""
    opens = {"n": 0}

    def counting():
        opens["n"] += 1
        return _FakeDriver()

    monkeypatch.setattr(wedge1_mcp, "open_driver", counting)

    async def go():
        for _ in range(5):
            await wedge1_mcp.driver()

    asyncio.run(go())
    assert opens["n"] == 1, f"opened the store {opens['n']} times for 5 queries"


def test_concurrent_calls_open_once(monkeypatch):
    """Concurrent tool calls must not each try to open the store.

    Kuzu would reject the losers with GraphLocked -- against our own driver.
    """
    opens = {"n": 0}

    def slow_counting():
        opens["n"] += 1
        time.sleep(0.2)
        return _FakeDriver()

    monkeypatch.setattr(wedge1_mcp, "open_driver", slow_counting)

    async def go():
        await asyncio.gather(*(wedge1_mcp.driver() for _ in range(8)))

    asyncio.run(go())
    assert opens["n"] == 1, f"{opens['n']} concurrent opens; the lock did not serialise"


class _FakeDriver:
    """Stands in for KuzuDriver: records that close() was awaited."""

    def __init__(self):
        self.closed = False

    async def build_indices_and_constraints(self):
        return None

    async def close(self):
        self.closed = True
