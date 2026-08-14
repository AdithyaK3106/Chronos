"""The resident Chronos process.

Run: python -m chronos.daemon.server

What it actually buys, measured on this machine before it was written:

    bare interpreter                 70 ms
    import chronos.cli             5039 ms   <- every CLI invocation pays this
      of which graphiti_core       3900 ms   (pulls in openai + neo4j)
      Kuzu driver open on top        ~0 ms

So the expensive thing is the IMPORT CHAIN, not the driver -- the driver is
cheap once graphiti is loaded. The daemon pays both once at startup and holds
them; clients stay on the stdlib-only side and talk JSON over loopback.

Concurrency: one lock around all graph work. Kuzu is embedded and its driver is
not safe for concurrent writers, and enforce reads the same store enforcement
writes provenance into. Requests are accepted concurrently and serialized here.
# ponytail: one global lock, per-repo locks if a partner ever runs enough
# parallel enforces for the queue to matter.
"""

import json
import os
import socket
import sys
import threading
import time
import traceback

from . import protocol


class ChronosDaemon:
    def __init__(self, host: str = protocol.HOST, port: int = protocol.DEFAULT_PORT):
        self._driver = None
        self._lock = threading.Lock()      # serializes all graph work
        self._sock = None
        self._running = True
        self._host = host
        self._port = port
        self._started_at = None
        self._served = 0
        self._loop = None                  # one event loop, reused per request

    # -- lifecycle --------------------------------------------------------

    def start(self) -> int:
        self._started_at = time.time()
        self._open_driver()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._host, self._port))
        port = self._sock.getsockname()[1]
        self._sock.listen(16)

        state = {"port": port, "pid": os.getpid(), "started_at": self._started_at}
        sf = protocol.state_file()
        sf.parent.mkdir(parents=True, exist_ok=True)
        sf.write_text(json.dumps(state), encoding="utf-8")

        # The parent blocks on this line to learn the port, so it must be
        # flushed immediately and must be the first thing on stdout.
        print(f"{protocol.READY_PREFIX} port={port} pid={os.getpid()}", flush=True)

        # settimeout makes accept() interruptible, so `shutdown` takes effect
        # within a second instead of blocking until the next connection.
        self._sock.settimeout(1.0)
        try:
            while self._running:
                try:
                    conn, _ = self._sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True, name="chronos-daemon-conn").start()
        finally:
            self._cleanup(sf)
        return 0

    def _cleanup(self, state_path):
        # Only remove the state file if it is still ours: a second daemon that
        # took over would otherwise be un-discoverable after this one exits.
        try:
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if int(state.get("pid", -1)) == os.getpid():
                    state_path.unlink()
        except (OSError, ValueError):
            pass
        try:
            if self._sock:
                self._sock.close()
        except OSError:
            pass
        if self._driver is not None:
            try:
                self._run(self._driver.close())
            except Exception:  # noqa: BLE001 -- best effort on the way out
                pass
        if self._loop is not None:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    def _open_driver(self):
        """Pay the expensive imports and open the graph, once.

        Kuzu takes an EXCLUSIVE file lock on the store, so exactly one process
        may hold it: a second daemon, or a `chronos index` run started while a
        daemon is up, fails here. That is a real constraint of the embedded
        store, not something the daemon can work around, so it is reported as
        an instruction rather than a traceback.
        """
        import asyncio

        from chronos.store import db_path, ensure_schema, open_driver

        self._loop = asyncio.new_event_loop()
        try:
            self._driver = open_driver()
        except RuntimeError as e:
            if "lock" not in str(e).lower():
                raise
            print(f"chronos daemon: cannot open the graph at {db_path()} -- "
                  f"another process holds it.\n"
                  f"  Another daemon is probably already running "
                  f"(python -m chronos daemon status),\n"
                  f"  or a direct `chronos index`/`sync` is in flight. "
                  f"Stop it and retry.\n  {e}", file=sys.stderr)
            raise SystemExit(2) from None
        self._run(ensure_schema(self._driver))

    def _run(self, coro):
        """Run a coroutine on the daemon's own loop.

        One long-lived loop rather than asyncio.run() per request: asyncio.run
        creates and tears down a loop each time, and the Kuzu driver holds
        state tied to the loop it was opened on.
        """
        return self._loop.run_until_complete(coro)

    # -- connection handling ----------------------------------------------

    def _handle(self, conn: socket.socket):
        rid = ""
        try:
            req = protocol.read_message(conn, timeout=30.0)
            if req is None:
                return                     # closed or malformed; nothing to reply to
            rid = req.get("id", "")
            resp = self._dispatch(req)
        except Exception as e:  # noqa: BLE001 -- a bad request must not kill the daemon
            traceback.print_exc(file=sys.stderr)
            resp = protocol.response(rid, error=f"{type(e).__name__}: {e}")
        try:
            conn.sendall(protocol.encode(resp))
        except OSError:
            pass                           # client hung up; nothing to do
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, req: dict) -> dict:
        rid = req.get("id", "")
        method = req.get("method")
        params = req.get("params") or {}

        if req.get("v") != protocol.PROTOCOL_VERSION:
            return protocol.response(
                rid, error=f"unsupported protocol version {req.get('v')!r}; "
                           f"daemon speaks {protocol.PROTOCOL_VERSION}. Restart the daemon.")

        self._served += 1
        try:
            if method == "ping":
                return protocol.response(rid, {
                    "pong": True, "pid": os.getpid(),
                    "uptime_s": round(time.time() - self._started_at, 1),
                    "served": self._served,
                })
            if method == "shutdown":
                self._running = False
                return protocol.response(rid, {"ok": True})
            if method == "enforce":
                return protocol.response(rid, self._do_enforce(params))
            if method == "index":
                return protocol.response(rid, self._do_index(params))
            return protocol.response(rid, error=f"unknown method: {method!r}")
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return protocol.response(rid, error=f"{type(e).__name__}: {e}")

    # -- methods ----------------------------------------------------------

    def _do_enforce(self, params: dict) -> dict:
        from chronos.cli import enforce_files, load_repo_config, select_files

        repo = params.get("repo") or "."
        # Same config resolution the CLI does, so daemon and direct paths read
        # the same rule store (the pilot found them disagreeing when they did not).
        load_repo_config(repo)
        files = select_files(repo, params.get("file"), params.get("diff"))
        if not files:
            return {"rows": [], "blocks": 0, "warns": 0, "oks": 0,
                    "checked": 0, "skipped": 0, "empty": True}
        with self._lock:
            report = self._run(enforce_files(
                files, repo, lang=params.get("lang"),
                group=params.get("group") or "default",
                agent_id=params.get("agent_id"), session_id=params.get("session_id"),
                driver=self._driver))
        report["empty"] = False
        return report

    def _do_index(self, params: dict) -> dict:
        from chronos.cli import commit_time, load_repo_config
        from chronos.indexer import index_repo_graph
        from chronos.sync import Syncer

        repo = params.get("repo") or "."
        group = params.get("group") or "default"
        load_repo_config(repo)
        t0 = time.time()
        # The C indexer subprocess dominates this (~18-25s on a 2.4k-node repo);
        # the daemon saves the import cost, not the indexing cost. Held outside
        # the lock deliberately -- it touches no graph state.
        nodes, edges = index_repo_graph(repo, mode=params.get("mode") or "fast")
        indexed_s = time.time() - t0
        at = commit_time(repo)
        with self._lock:
            st = self._run(Syncer(self._driver, group).sync(nodes, edges, at))
        return {"nodes": len(nodes), "edges": len(edges),
                "added": st.edges_added, "invalidated": st.edges_invalidated,
                "unchanged": st.edges_unchanged, "group": group, "at": at.isoformat(),
                "indexed_s": round(indexed_s, 1),
                "elapsed_s": round(time.time() - t0, 2)}


def main() -> int:
    return ChronosDaemon().start()


if __name__ == "__main__":
    sys.exit(main())
