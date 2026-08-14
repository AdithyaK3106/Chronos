"""Wire format shared by the daemon and its clients.

Stdlib only, and deliberately so: this module is imported by the client on the
fast path, where every import is charged to a pre-commit hook.

Transport is TCP on 127.0.0.1 rather than a Unix socket or a named pipe --
one implementation that behaves the same on Windows, macOS and Linux. The port
is OS-assigned and published in the state file, so nothing is hardcoded and two
checkouts can run daemons side by side.
"""

import json
import os
from pathlib import Path

PROTOCOL_VERSION = "1"
HOST = "127.0.0.1"
DEFAULT_PORT = 0        # 0 = let the OS pick; the real port goes in the state file
READY_PREFIX = "CHRONOS_DAEMON_READY"
DISABLE_ENV = "CHRONOS_DAEMON"   # set to 0/false/no/off to bypass the daemon
MAX_MESSAGE_BYTES = 8 * 1024 * 1024   # refuse to buffer a runaway sender forever


def state_file() -> Path:
    """Where the daemon publishes its port and pid.

    Read at call time, not import time: the tests point CHRONOS_HOME at a tmp
    dir, and a module-level constant would freeze the real home path into the
    module before they get the chance.
    """
    home = os.environ.get("CHRONOS_HOME")
    base = Path(home) if home else Path.home() / ".chronos"
    return base / "daemon.json"


# Kept as a module attribute because the spec names it; state_file() is the
# accessor everything should actually use.
DAEMON_STATE_FILE = state_file()


def daemon_disabled() -> bool:
    """True when the operator has opted out. Checked on every call so the
    switch works without restarting anything."""
    return os.environ.get(DISABLE_ENV, "").strip().lower() in ("0", "false", "no", "off")


def request(method: str, params: dict | None = None, rid: str | None = None) -> dict:
    import uuid
    return {"v": PROTOCOL_VERSION, "id": rid or str(uuid.uuid4()),
            "method": method, "params": params or {}}


def response(rid: str, result=None, error: str | None = None) -> dict:
    return {"v": PROTOCOL_VERSION, "id": rid, "result": result, "error": error}


def encode(msg: dict) -> bytes:
    """One JSON object, one line. Newline is the frame delimiter, so the payload
    must not contain a raw one -- json.dumps escapes them by default."""
    return (json.dumps(msg) + "\n").encode("utf-8")


def read_message(sock, timeout: float | None = None) -> dict | None:
    """Read exactly one newline-delimited JSON message.

    Returns None on a closed connection or a malformed frame. Reads until the
    delimiter rather than assuming one recv() holds the whole message -- a
    2KB enforce result arrives in several chunks over loopback.
    """
    if timeout is not None:
        sock.settimeout(timeout)
    buf = bytearray()
    while b"\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except OSError:
            return None
        if not chunk:
            return None          # peer closed before completing a frame
        buf += chunk
        if len(buf) > MAX_MESSAGE_BYTES:
            return None          # runaway/garbage sender: drop it, never buffer forever
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        msg = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) else None
