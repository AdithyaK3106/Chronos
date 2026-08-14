"""Chronos daemon: lifecycle, protocol, and the fallback contract.

Run: pytest tests/test_daemon.py -v

The daemon is an optimisation, so the property under test is not "it is fast"
but "it is never the reason something breaks". Most of these tests are about
what happens when it is absent, dead, or wrong.

Each test that needs a daemon starts its own on an isolated CHRONOS_HOME, so
the suite never talks to a developer's real daemon or clobbers their state file.
"""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos.daemon import protocol  # noqa: E402
from chronos.daemon.client import DaemonClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
START_TIMEOUT_S = 90     # startup is the ~4s import chain, plus cold-disk variance


class Daemon:
    """A daemon in its own CHRONOS_HOME, torn down on exit."""

    def __init__(self, home: Path):
        self.home = home
        self.proc = None
        self.port = None
        self.pid = None

    def __enter__(self):
        # Kuzu takes an exclusive lock on the graph, so a test daemon MUST get
        # its own store or it collides with the developer's running daemon.
        # CHRONOS_DB (graph) and CHRONOS_SQLITE (rules/ledger) are both pointed
        # inside the isolated home.
        env = {**os.environ, "CHRONOS_HOME": str(self.home),
               "CHRONOS_DB": str(self.home / "graph.kz"),
               "CHRONOS_SQLITE": str(self.home / "chronos.db")}
        env.pop("CHRONOS_DAEMON", None)      # never inherit an opt-out
        env.pop("CHRONOS_LEDGER", None)      # legacy var would override the above
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "chronos.daemon.server"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            cwd=str(REPO_ROOT), env=env)
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"daemon exited {self.proc.returncode}: "
                    f"{(self.proc.stderr.read() or '')[-2000:]}")
            line = self.proc.stdout.readline()
            if line.startswith(protocol.READY_PREFIX):
                parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
                self.port = int(parts["port"])
                self.pid = int(parts["pid"])
                return self
        raise TimeoutError("daemon never reported ready")

    def __exit__(self, *exc):
        if self.proc and self.proc.poll() is None:
            try:
                DaemonClient(port=self.port).shutdown()
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                self.proc.kill()
                self.proc.wait(timeout=10)
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def client(self) -> DaemonClient:
        return DaemonClient(port=self.port)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolate the state file so tests never touch the real ~/.chronos."""
    h = tmp_path / "chronos_home"
    h.mkdir()
    monkeypatch.setenv("CHRONOS_HOME", str(h))
    monkeypatch.delenv("CHRONOS_DAEMON", raising=False)
    return h


# --- 1. lifecycle ---------------------------------------------------------

def test_start_ping_stop(home):
    """Start, answer a ping, shut down, and clean up after itself."""
    with Daemon(home) as d:
        state_path = home / "daemon.json"
        assert state_path.is_file(), "daemon must publish its port"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        assert state["port"] == d.port and state["pid"] == d.pid

        info = d.client().ping()
        assert info is not None and info["pong"] is True
        assert info["pid"] == d.pid

        assert d.client().shutdown() is True
        d.proc.wait(timeout=15)

    # 6. state file is removed on shutdown, so nothing points at a dead port.
    assert not (home / "daemon.json").exists(), "stale state file left behind"


def test_shutdown_is_idempotent_from_the_clients_view(home):
    """A second stop against a gone daemon reports absence, never an exception."""
    with Daemon(home) as d:
        c = d.client()
        assert c.shutdown() is True
        d.proc.wait(timeout=15)
        assert c.available() is False
        assert c.shutdown() is False


# --- 2. enforce over the wire --------------------------------------------

def test_enforce_via_daemon_matches_the_direct_result(home, tmp_path):
    """A real enforce round-trip: the daemon runs the same code the CLI does.

    Not mocked. Mocking enforce() inside the daemon would need the mock to
    exist in the daemon's process, so it would only prove the socket works --
    the thing worth proving is that the shared code path returns a real report.
    """
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    with Daemon(home) as d:
        report = d.client().enforce(repo=str(repo), file="src/a.py", lang="python")
    assert report is not None, "daemon returned no report"
    for key in ("rows", "blocks", "warns", "oks", "checked", "skipped"):
        assert key in report, f"missing {key}"
    assert report["checked"] == 1
    assert report["blocks"] == 0


def test_enforce_reports_empty_selection_instead_of_failing(home, tmp_path):
    """A file that does not exist yields an empty check, not an error."""
    repo = tmp_path / "repo"
    repo.mkdir()
    with Daemon(home) as d:
        report = d.client().enforce(repo=str(repo), file="nope.py", lang="python")
    assert report is not None
    assert report["checked"] == 0


def test_unknown_method_is_an_error_not_a_crash(home):
    """A bad method must not take the daemon down."""
    with Daemon(home) as d:
        c = d.client()
        assert c._call("no_such_method", {}) is None
        assert "unknown method" in (c.last_error or "")
        assert c.ping() is not None, "daemon died on an unknown method"


def test_protocol_version_mismatch_is_rejected_clearly(home):
    """A client from a different version gets told to restart, not ignored."""
    with Daemon(home) as d:
        req = {"v": "999", "id": "x", "method": "ping", "params": {}}
        with socket.create_connection((protocol.HOST, d.port), timeout=10) as s:
            s.sendall(protocol.encode(req))
            resp = protocol.read_message(s, timeout=10)
        assert resp["error"] and "protocol version" in resp["error"]
        assert d.client().ping() is not None


# --- 3/4. the fallback contract ------------------------------------------

def test_client_is_unavailable_with_no_daemon(home):
    """No daemon: available() is False and calls return None, never raise."""
    c = DaemonClient()
    assert c.available() is False
    assert c.enforce(repo=".", file="x.py", lang="python") is None
    assert c.index(repo=".") is None
    assert c.ping() is None


def test_client_returns_none_when_daemon_dies_mid_flight(home):
    """A killed daemon costs latency, not an exception."""
    d = Daemon(home).__enter__()
    c = d.client()
    assert c.ping() is not None
    d.proc.kill()
    d.proc.wait(timeout=15)
    assert c.enforce(repo=".", file="x.py", lang="python") is None
    assert c.available() is False


def test_stale_state_file_does_not_make_the_client_believe(home):
    """A state file outliving its daemon must not route calls into a void."""
    (home / "daemon.json").write_text(
        json.dumps({"port": 9, "pid": 999999, "started_at": time.time()}),
        encoding="utf-8")
    c = DaemonClient()
    assert c.port == 9, "port should be read"
    assert c.available() is False, "ping must decide, not the file"


def test_corrupt_state_file_is_ignored(home):
    (home / "daemon.json").write_text("{not json", encoding="utf-8")
    assert DaemonClient().available() is False


def test_disable_env_bypasses_a_running_daemon(home, monkeypatch):
    """CHRONOS_DAEMON=0 must be honoured even when a daemon is up."""
    with Daemon(home) as d:
        assert d.client().available() is True
        monkeypatch.setenv("CHRONOS_DAEMON", "0")
        assert d.client().available() is False
        assert d.client().enforce(repo=".", file="x.py", lang="python") is None


# --- 5. concurrency -------------------------------------------------------

def test_concurrent_pings(home):
    """10 simultaneous clients: all answered, no crash, no deadlock."""
    with Daemon(home) as d:
        results, errors = [], []

        def hit():
            try:
                results.append(d.client().ping())
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=hit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"client raised under concurrency: {errors}"
        assert len(results) == 10
        assert all(r and r["pong"] is True for r in results)
        assert d.client().ping() is not None, "daemon unhealthy after concurrency"


def test_concurrent_enforces_are_serialized_without_error(home, tmp_path):
    """Graph work is behind one lock; parallel callers must still all get a report."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    with Daemon(home) as d:
        out = []

        def hit():
            out.append(d.client().enforce(repo=str(repo), file="src/a.py", lang="python"))

        threads = [threading.Thread(target=hit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(out) == 5 and all(r is not None for r in out)
        assert all(r["checked"] == 1 for r in out)


# --- rendering parity -----------------------------------------------------

def test_fast_path_renderer_matches_the_cli_renderer(capsys):
    """__main__ duplicates the report renderer to avoid importing chronos.cli
    (which costs ~5s). Duplication is only safe if the two stay identical."""
    from chronos import __main__ as fast
    from chronos.cli import print_enforce_report

    report = {"rows": [{"file": "a.py", "verdict": "ok"},
                       {"file": "b.py", "verdict": "warn", "line": "12",
                        "rule_id": "r1", "message": "some message"},
                       {"file": "c.py", "verdict": "block", "line": "",
                        "rule_id": "r2", "message": "blocked"}],
              "blocks": 1, "warns": 1, "oks": 1, "checked": 3, "skipped": 2}

    print_enforce_report(report)
    a = capsys.readouterr().out
    fast._print_report(report)
    b = capsys.readouterr().out
    assert a == b, "daemon and direct paths would print different output"


def test_fast_path_declines_unknown_flags():
    """Anything the fast path does not fully understand goes to argparse."""
    from chronos import __main__ as fast
    assert fast._fast_path(["enforce", "--totally-unknown", "x"]) is None
    assert fast._fast_path(["enforce", "--repo"]) is None      # missing value
    assert fast._fast_path(["gc"]) is None                     # not a hot command


def test_fast_path_is_skipped_when_disabled(monkeypatch):
    monkeypatch.setenv("CHRONOS_DAEMON", "0")
    from chronos import __main__ as fast
    assert fast._fast_path(["enforce", "--repo", "."]) is None


# --- protocol -------------------------------------------------------------

def test_read_message_handles_a_split_frame():
    """A response arrives in several recv() chunks; framing must not assume one."""
    a, b = socket.socketpair()
    try:
        payload = protocol.encode({"v": "1", "id": "x", "result": {"k": "y" * 5000},
                                   "error": None})
        threading.Thread(target=lambda: (a.sendall(payload[:100]),
                                         time.sleep(0.05),
                                         a.sendall(payload[100:])),
                         daemon=True).start()
        msg = protocol.read_message(b, timeout=10)
        assert msg and msg["id"] == "x"
    finally:
        a.close()
        b.close()


def test_read_message_returns_none_on_garbage():
    a, b = socket.socketpair()
    try:
        a.sendall(b"this is not json\n")
        assert protocol.read_message(b, timeout=5) is None
    finally:
        a.close()
        b.close()


# --- Kuzu's exclusive lock: the daemon's main operational consequence ------

def test_graph_locked_is_a_typed_error_with_a_remedy(home, monkeypatch):
    """Kuzu allows one process to hold the store. With a daemon running, every
    direct graph command hits this, so it must arrive as GraphLocked carrying
    the fix -- not as a raw kuzu RuntimeError."""
    from chronos.store import GraphLocked, open_driver

    monkeypatch.setenv("CHRONOS_DB", str(home / "graph.kz"))
    monkeypatch.delenv("CHRONOS_DB_URI", raising=False)
    holder = open_driver()          # take the lock in this process
    try:
        code = ("import os,sys;"
                "sys.path.insert(0,r'%s');"
                "os.environ['CHRONOS_DB']=r'%s';"
                "from chronos.store import open_driver, GraphLocked;"
                "\ntry:\n open_driver()\n print('NO_ERROR')\n"
                "except GraphLocked as e:\n print('GRAPH_LOCKED');print(e)\n"
                % (REPO_ROOT, home / "graph.kz"))
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=180)
        assert "GRAPH_LOCKED" in out.stdout, f"got: {out.stdout}{out.stderr[-500:]}"
        assert "daemon stop" in out.stdout, "the error must name the remedy"
    finally:
        import asyncio
        asyncio.run(holder.close())
        assert GraphLocked is not None
