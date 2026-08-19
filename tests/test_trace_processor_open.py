"""_resolve_touched's own open_driver() call must be bounded.

Run: pytest tests/test_trace_processor_open.py -v

Kuzu's lock is inconsistent (see wedge1_mcp.driver()'s docstring): it can
block inside the C layer with no exception at all instead of raising
GraphLocked. _resolve_touched runs on a background thread with no timeout
of its own -- an unbounded block there starves wedge1_mcp.driver()'s own
open of the same OS-level lock for the rest of its 90s budget. Reproduced
2026-08-19 by racing index_health against a concurrent trace_processor burst:
index_health hit the 90s "leaked thread" path.

FIX HISTORY, since the first attempt here was wrong and worth recording: an
`asyncio.wait_for(asyncio.to_thread(open_driver), timeout=...)` wrapper looks
bounded but isn't -- asyncio.to_thread's worker still has to be joined before
asyncio.run() can return, so the timeout only stops the await, not the
enclosing call. test_blocked_open_does_not_stall_the_caller caught this: it
measured the full 10s block even with the wrapper in place. The shipped fix
runs the open on a plain daemon thread that reports back via a Queue, and
_resolve_touched waits on the queue with a real timeout -- no join, so a
stuck open leaks its thread (accepted, same as wedge1_mcp.driver()'s
TimeoutError branch) without blocking the caller.
"""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import trace_processor  # noqa: E402
from chronos.store import GraphLocked  # noqa: E402


class _FakeDriver:
    async def close(self):
        pass


def _run_resolve_touched(payload, timeout=2):
    t0 = time.monotonic()
    worker = threading.Thread(
        target=trace_processor._resolve_touched, args=("group", {}, payload), daemon=True)
    worker.start()
    worker.join(timeout=timeout)
    return worker.is_alive(), time.monotonic() - t0


def test_blocked_open_does_not_stall_the_caller(monkeypatch):
    """A same-process open that never returns must not stall whatever thread
    dispatched _resolve_touched -- it must be isolated to _resolve_touched's
    own (daemon) thread, the way _dispatch_to_reflector already runs it."""
    monkeypatch.setattr(trace_processor, "_RESOLVE_OPEN_TIMEOUT", 0.3)
    monkeypatch.setattr("chronos.store.open_driver", lambda: time.sleep(10))
    monkeypatch.setattr(trace_processor, "candidate_symbols", lambda trace: ["some.symbol"])

    # to_reflector_trace always seeds nodes_touched=[] before _resolve_touched
    # runs; on the timeout path _resolve_touched leaves that default alone
    # rather than writing to payload from a thread the caller has given up on.
    payload = {"nodes_touched": []}
    still_alive, elapsed = _run_resolve_touched(payload)

    assert not still_alive, (
        f"_resolve_touched did not return within 2s -- its own timeout "
        f"({trace_processor._RESOLVE_OPEN_TIMEOUT}s) was not enforced")
    assert elapsed < 2
    assert payload["nodes_touched"] == []


def test_graph_locked_is_swallowed(monkeypatch):
    """A same-process GraphLocked (see test_mcp_driver.py's transient-lock
    race) must still degrade to an empty result, not raise or hang."""
    monkeypatch.setattr(trace_processor, "_RESOLVE_OPEN_TIMEOUT", 5.0)

    def raises_locked():
        raise GraphLocked("the graph is locked by another process.")

    monkeypatch.setattr("chronos.store.open_driver", raises_locked)
    monkeypatch.setattr(trace_processor, "candidate_symbols", lambda trace: ["some.symbol"])

    payload = {"nodes_touched": []}
    still_alive, elapsed = _run_resolve_touched(payload)

    assert not still_alive
    assert elapsed < 2
    assert payload["nodes_touched"] == []


def test_successful_resolution_fills_payload(monkeypatch):
    """The ordinary path: open succeeds, nodes resolve, payload is filled."""
    monkeypatch.setattr(trace_processor, "_RESOLVE_OPEN_TIMEOUT", 5.0)
    monkeypatch.setattr("chronos.store.open_driver", lambda: _FakeDriver())
    monkeypatch.setattr(trace_processor, "candidate_symbols", lambda trace: ["some.symbol"])

    async def fake_resolve_nodes(driver, group_id, names):
        return ["resolved::node"]

    monkeypatch.setattr(trace_processor, "resolve_nodes", fake_resolve_nodes)

    payload = {"nodes_touched": []}
    still_alive, elapsed = _run_resolve_touched(payload)

    assert not still_alive
    assert elapsed < 2
    assert payload["nodes_touched"] == ["resolved::node"]


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
