"""GraphLocked must never blame the daemon by default.

Run: pytest tests/test_graph_locked.py -v

THE BUG THIS PINS
------------------
Under multi-agent load (concurrent `chronos index` runs, no daemon), the lock
error unconditionally said "a Chronos daemon is the usual holder" and offered
`daemon stop` -- wrong when the real holders are peer index/mcp processes and
`daemon status` reports not running. A diagnostic that confidently names the
wrong culprit is worse than one that admits it does not know.
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import store  # noqa: E402


class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout


def test_no_daemon_claim_when_holder_is_a_peer_process(monkeypatch):
    """Concurrent `chronos index` processes hold the lock; message must name
    them, not the daemon."""
    def fake_run(cmd, **kw):
        if sys.platform == "win32":
            return _FakeCompleted(
                "Node,CommandLine,ProcessId\r\n"
                ",python -m chronos index --repo C:\\p\\a,4821\r\n"
                ",python -m chronos index --repo C:\\p\\b,4900\r\n")
        return _FakeCompleted(
            "4821 python -m chronos index --repo /p/a\n"
            "4900 python -m chronos index --repo /p/b\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    hint = store._holder_hint()
    assert "daemon" not in hint.lower(), hint
    assert "4821" in hint and "4900" in hint, hint


def test_holder_unknown_when_nothing_found(monkeypatch):
    """No matching process anywhere -- admit it, do not default to daemon."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeCompleted(""))
    hint = store._holder_hint()
    assert "daemon" not in hint.lower(), hint
    assert "unknown" in hint.lower(), hint


def test_open_driver_message_is_conditional_on_daemon(monkeypatch):
    """The message must gate the daemon remedy behind 'if', never assert it."""
    monkeypatch.setattr(store, "_holder_hint", lambda: "  Holder unknown.\n")

    class _LockedKuzu:
        def __init__(self, *a, **kw):
            raise RuntimeError("could not acquire lock on database")

    import warnings
    monkeypatch.setattr(store.os.environ, "get", store.os.environ.get)
    with pytest.raises(store.GraphLocked) as ei:
        with warnings.catch_warnings():
            from graphiti_core.driver import kuzu_driver
            monkeypatch.setattr(kuzu_driver, "KuzuDriver", _LockedKuzu)
            store.open_driver()
    msg = str(ei.value)
    assert "If a daemon is the holder" in msg, msg
    assert "Holder unknown" in msg, msg
