"""Wedge 2 automatic capture: pytest plugin + trace processor.

Run: pytest tests/test_pytest_plugin.py -v

No LLM, no network. The Reflector dispatch is mocked everywhere — this suite
covers capture and routing, not reflection quality (test_wedge2.py owns that).
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import pytest_plugin, trace_processor  # noqa: E402

pytest_plugins = ["pytester"]


def _write_trace(tmp_path, **overrides):
    trace = {
        "source": "pytest",
        "session_start": datetime.now(timezone.utc).isoformat(),
        "session_end": datetime.now(timezone.utc).isoformat(),
        "exit_status": 1,
        "total_failed": 2,
        "failures": [{"test_id": "tests/test_x.py::test_a", "outcome": "failed",
                      "message": "AssertionError: boom", "traceback": "tb",
                      "duration_s": 0.01,
                      "captured_at": datetime.now(timezone.utc).isoformat()}],
        "repo_path": str(tmp_path),
        "cwd": str(tmp_path),
    }
    trace.update(overrides)
    d = tmp_path / ".chronos" / "traces"
    d.mkdir(parents=True, exist_ok=True)
    (d / "pending.jsonl").write_text(json.dumps(trace) + "\n", encoding="utf-8")
    return d / "pending.jsonl"


def test_plugin_writes_pending_on_failure(pytester):
    """A failing test run leaves a trace behind."""
    pytester.makeconftest('pytest_plugins = ["chronos.pytest_plugin"]')
    (pytester.path / ".chronos").mkdir()          # marks this as the repo root
    pytester.makepyfile("def test_fails():\n    assert 1 == 2, 'deliberate'\n")

    result = pytester.runpytest_subprocess()
    result.assert_outcomes(failed=1)

    pending = pytester.path / ".chronos" / "traces" / "pending.jsonl"
    assert pending.exists(), "no trace written for a failing run"
    trace = json.loads(pending.read_text(encoding="utf-8").strip())
    assert trace["source"] == "pytest"
    assert trace["total_failed"] > 0
    assert trace["failures"][0]["test_id"].endswith("::test_fails")
    assert "deliberate" in trace["failures"][0]["message"]


def test_plugin_silent_on_clean_run(pytester):
    """A green run must leave nothing behind — no lesson in a pass."""
    pytester.makeconftest('pytest_plugins = ["chronos.pytest_plugin"]')
    (pytester.path / ".chronos").mkdir()
    pytester.makepyfile("def test_passes():\n    assert True\n")

    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)

    pending = pytester.path / ".chronos" / "traces" / "pending.jsonl"
    assert not pending.exists(), "clean run should not write a trace"


def test_foreign_chronos_sqlite_does_not_divert_traces(pytester, monkeypatch):
    """A CHRONOS_SQLITE pointing at an unrelated repo must be ignored.

    Regression: a stale env var silently redirected traces out of the repo
    under test, losing every lesson with no error anywhere."""
    monkeypatch.setenv("CHRONOS_SQLITE", r"C:\somewhere\else\.chronos\chronos.db")
    pytester.makeconftest('pytest_plugins = ["chronos.pytest_plugin"]')
    (pytester.path / ".chronos").mkdir()
    pytester.makepyfile("def test_fails():\n    assert 1 == 2, 'deliberate'\n")

    pytester.runpytest_subprocess().assert_outcomes(failed=1)

    pending = pytester.path / ".chronos" / "traces" / "pending.jsonl"
    assert pending.exists(), "trace was diverted out of the repo under test"


def test_traceback_is_capped(tmp_path):
    """A giant longrepr must not blow up pending.jsonl."""
    plugin = pytest_plugin.ChronosCapturePlugin(tmp_path)

    class FakeReport:
        when = "call"
        failed = True
        nodeid = "tests/test_big.py::test_big"
        longrepr = "X" * 10000
        duration = 0.5

    plugin.pytest_runtest_logreport(FakeReport())
    assert len(plugin.failures) == 1
    tb = plugin.failures[0]["traceback"]
    assert len(tb) <= pytest_plugin.MAX_TRACEBACK_CHARS
    assert len(tb) == pytest_plugin.MAX_TRACEBACK_CHARS


def test_failure_count_is_capped(tmp_path):
    """A 2000-test wipeout is one bug, not 2000 lessons."""
    plugin = pytest_plugin.ChronosCapturePlugin(tmp_path)

    class FakeReport:
        when = "call"
        failed = True
        nodeid = "tests/test_x.py::test_x"
        longrepr = "boom"
        duration = 0.1

    for _ in range(pytest_plugin.MAX_FAILURES + 25):
        plugin.pytest_runtest_logreport(FakeReport())
    assert len(plugin.failures) == pytest_plugin.MAX_FAILURES


def test_process_pending_dispatches_and_clears(tmp_path, monkeypatch):
    pending = _write_trace(tmp_path)
    calls = []
    monkeypatch.setattr(trace_processor, "_dispatch_to_reflector",
                        lambda t: calls.append(t))

    n = trace_processor.process_pending(tmp_path)

    assert n == 1
    assert len(calls) == 1
    assert calls[0]["total_failed"] == 2
    assert pending.read_text(encoding="utf-8").strip() == "", \
        "dispatched trace must be removed from pending.jsonl"


def test_process_pending_discards_stale(tmp_path, monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    pending = _write_trace(tmp_path, session_end=stale)
    calls = []
    monkeypatch.setattr(trace_processor, "_dispatch_to_reflector",
                        lambda t: calls.append(t))

    n = trace_processor.process_pending(tmp_path)

    assert n == 0
    assert calls == [], "a >24h trace must not reach the Reflector"
    assert pending.read_text(encoding="utf-8").strip() == ""


def test_process_pending_missing_file_is_zero(tmp_path):
    assert trace_processor.process_pending(tmp_path) == 0


def test_malformed_line_never_raises(tmp_path, monkeypatch):
    d = tmp_path / ".chronos" / "traces"
    d.mkdir(parents=True)
    (d / "pending.jsonl").write_text("{not json\n", encoding="utf-8")
    monkeypatch.setattr(trace_processor, "_dispatch_to_reflector", lambda t: None)
    assert trace_processor.process_pending(tmp_path) == 0


def test_low_signal_is_kept_not_dispatched(tmp_path, monkeypatch):
    """A trace with no failures stays on disk until it ages out."""
    pending = _write_trace(tmp_path, source="bash", stdout="all good",
                           stderr="", total_failed=0)
    calls = []
    monkeypatch.setattr(trace_processor, "_dispatch_to_reflector",
                        lambda t: calls.append(t))

    n = trace_processor.process_pending(tmp_path)

    assert n == 0 and calls == []
    assert pending.read_text(encoding="utf-8").strip() != "", "low-signal must be kept"


def test_to_reflector_trace_shape():
    """The mapped dict must carry every key reflector.reflect() reads."""
    trace = {
        "source": "pytest", "total_failed": 1, "repo_path": "/repo",
        "session_start": "2026-08-14T00:00:00+00:00",
        "session_end": "2026-08-14T00:01:00+00:00",
        "failures": [{"test_id": "tests/t.py::test_a",
                      "message": "AssertionError: boom", "traceback": "tb"}],
    }
    out = trace_processor.to_reflector_trace(trace)
    for key in ("agent_id", "session_id", "action", "outcome",
                "error_or_correction", "nodes_touched", "timestamp"):
        assert key in out, f"missing {key} — reflector.reflect() reads it"
    assert out["outcome"] == "failed"
    assert "tests/t.py::test_a" in out["error_or_correction"]
    assert out["nodes_touched"] == [], "test ids are not graph qualified_names"


def test_bash_trace_is_high_signal_on_failure_text():
    assert trace_processor._is_high_signal(
        {"source": "bash", "stdout": "1 test FAILED", "stderr": ""})
    assert not trace_processor._is_high_signal(
        {"source": "bash", "stdout": "ok", "stderr": ""})


# --- Wedge 2 gap closed: pytest trace -> graph nodes -----------------------
# Auto-captured traces used to reach the Reflector with nodes_touched empty,
# so every auto lesson was ungrounded. Frames come from the traceback, never
# from the test id, and the graph decides which survive.

_TB = '''
def test_thing():
>       helper(1)

tests/t.py:7: in test_thing
    helper(1)
chronos/x.py:12: in helper
    return inner(v)
  File "chronos/x.py", line 20, in inner
    raise ValueError
E   ValueError
'''


def test_candidate_symbols_reads_frames_not_test_ids():
    names = trace_processor.candidate_symbols(
        {"failures": [{"test_id": "tests/t.py::test_thing", "traceback": _TB}]})
    # Every frame shape pytest emits is covered: `def`, `path:n: in f`, and the
    # stdlib `File ..., in f` form.
    assert "helper" in names and "inner" in names
    assert names.count("test_thing") == 1, "first-seen order, deduped"


def test_candidate_symbols_empty_without_a_traceback():
    assert trace_processor.candidate_symbols({"failures": []}) == []
    assert trace_processor.candidate_symbols(
        {"failures": [{"traceback": ""}]}) == []


def test_resolve_nodes_keeps_only_what_the_graph_knows():
    import asyncio

    class FakeDriver:
        pass

    async def fake_rows(driver, cypher, **kw):
        assert kw["names"] == ["helper", "nope"]
        return [{"name": "helper"}]

    from chronos import query
    orig = query._rows
    query._rows = fake_rows
    try:
        got = asyncio.run(trace_processor.resolve_nodes(
            FakeDriver(), "g", ["helper", "nope"]))
    finally:
        query._rows = orig
    # An unresolvable name is dropped rather than passed to ground(), which is
    # what keeps the lesson grounded instead of decorated.
    assert got == ["helper"]


def test_resolve_nodes_short_circuits_on_empty():
    import asyncio
    assert asyncio.run(trace_processor.resolve_nodes(None, "g", [])) == []
