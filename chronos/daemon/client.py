"""Thin client for the Chronos daemon. Never raises; degrades to None.

Every method returns None when the daemon is unavailable, unreachable, slow, or
reports an error, and the caller falls back to the in-process path. That
contract is the whole safety story: a broken daemon must cost latency, never
correctness -- a pre-commit hook that fails open because a background process
died would be worse than no daemon at all.

Stdlib imports only. This module is on the fast path.
"""

import json
import socket

from . import protocol

CONNECT_TIMEOUT_S = 1.0    # localhost: if it does not answer fast, it is not there
CALL_TIMEOUT_S = 120.0     # an index on a large repo legitimately takes minutes


class DaemonClient:
    def __init__(self, port: int | None = None):
        self._port = port if port is not None else self._read_port()
        self._last_error: str | None = None

    # -- state ------------------------------------------------------------

    @staticmethod
    def _read_port() -> int | None:
        try:
            state = json.loads(protocol.state_file().read_text(encoding="utf-8"))
            port = int(state["port"])
            return port if 0 < port < 65536 else None
        except (OSError, ValueError, KeyError, TypeError):
            return None      # no daemon, or a state file we cannot trust

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def last_error(self) -> str | None:
        """Why the most recent call failed. Diagnostics only -- callers branch
        on the None return, not on this."""
        return self._last_error

    def available(self) -> bool:
        """A live daemon answering on the published port.

        Pings rather than trusting the state file: a stale file outlives a
        killed daemon, and acting on it would send every call into a black hole.
        """
        if protocol.daemon_disabled() or not self._port:
            return False
        return self.ping() is not None

    # -- methods ----------------------------------------------------------

    def ping(self) -> dict | None:
        return self._call("ping", {}, timeout=CONNECT_TIMEOUT_S)

    def enforce(self, repo: str, file: str | None = None, lang: str | None = None,
                exit_code: bool = False, diff: str | None = None,
                group: str = "default", agent_id: str | None = None,
                session_id: str | None = None) -> dict | None:
        return self._call("enforce", {
            "repo": repo, "file": file, "lang": lang, "exit_code": exit_code,
            "diff": diff, "group": group,
            "agent_id": agent_id, "session_id": session_id,
        })

    def index(self, repo: str, group: str = "default", mode: str = "fast") -> dict | None:
        return self._call("index", {"repo": repo, "group": group, "mode": mode})

    def shutdown(self) -> bool:
        return self._call("shutdown", {}, timeout=CONNECT_TIMEOUT_S) is not None

    # -- transport --------------------------------------------------------

    def _call(self, method: str, params: dict, timeout: float = CALL_TIMEOUT_S) -> dict | None:
        self._last_error = None
        if protocol.daemon_disabled():
            self._last_error = f"{protocol.DISABLE_ENV} is set"
            return None
        if not self._port:
            self._last_error = "no daemon state file"
            return None
        req = protocol.request(method, params)
        try:
            with socket.create_connection((protocol.HOST, self._port),
                                          timeout=CONNECT_TIMEOUT_S) as s:
                s.settimeout(timeout)
                s.sendall(protocol.encode(req))
                resp = protocol.read_message(s, timeout=timeout)
            if resp is None:
                self._last_error = "no response (daemon died mid-call?)"
                return None
            if resp.get("id") != req["id"]:
                # One request per connection, so a mismatched id means we are
                # not talking to the daemon we think we are.
                self._last_error = "response id mismatch"
                return None
            if resp.get("error"):
                self._last_error = str(resp["error"])
                return None
            return resp.get("result")
        except OSError as e:
            self._last_error = f"{type(e).__name__}: {e}"
            return None
        except Exception as e:  # noqa: BLE001 -- the client must never raise
            self._last_error = f"{type(e).__name__}: {e}"
            return None
