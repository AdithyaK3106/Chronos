"""Graph store setup.

Kuzu is embedded (a file, not a server), which is what lets P0-5's "one install
step, no external services" actually hold. Upstream marks it deprecated, so it is
isolated here: swapping to FalkorDB/Neo4j means changing open_driver() only, and
CHRONOS_DB_URI already routes there.
"""

import os
import sys
from pathlib import Path


def db_path() -> Path:
    p = Path(os.environ.get("CHRONOS_DB", Path.home() / ".chronos" / "graph.kz"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class GraphLocked(RuntimeError):
    """The embedded graph is held by another process.

    Kuzu takes an exclusive file lock, so exactly one process may have the
    store open. Since the daemon holds it for its whole lifetime, every direct
    command would otherwise die on a raw kuzu traceback the moment a daemon is
    running. Raised as its own type so callers can say something useful.
    """


def open_driver():
    uri = os.environ.get("CHRONOS_DB_URI")
    if uri:  # e.g. bolt://... for Neo4j, when a partner outgrows the embedded store
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        return Neo4jDriver(uri, os.environ.get("CHRONOS_DB_USER", "neo4j"),
                           os.environ.get("CHRONOS_DB_PASSWORD", "password"))
    import warnings
    with warnings.catch_warnings():  # kuzu deprecation notice is expected and handled
        warnings.simplefilter("ignore")
        from graphiti_core.driver.kuzu_driver import KuzuDriver
        try:
            return KuzuDriver(db=str(db_path()))
        except RuntimeError as e:
            if "lock" not in str(e).lower():
                raise
            raise GraphLocked(
                f"the graph at {db_path()} is locked by another process.\n"
                f"  Kuzu allows one process at a time.\n"
                f"{_holder_hint()}"
                f"  If a daemon is the holder:\n"
                f"    python -m chronos daemon status   # see if one is running\n"
                f"    python -m chronos daemon stop     # release the graph\n"
                f"  Or set CHRONOS_DAEMON=0 and let the daemon serve the "
                f"command instead."
            ) from None


def _holder_hint() -> str:
    """Name the processes actually holding the graph, when we can find them.

    The old message asserted "a Chronos daemon is the usual holder" and offered
    `daemon stop`. That was wrong in the common multi-agent case: the holders
    were orphaned chronos-mcp servers from earlier sessions, `daemon status`
    said "not running", and the suggested remedy left the user stuck with no
    next step. A diagnostic that confidently names the wrong culprit is worse
    than one that admits it does not know.

    Best-effort: never let listing processes turn a lock error into a crash.
    """
    try:
        import subprocess
        me = os.getpid()
        procs = []  # (pid, command) pairs, any process whose cmdline mentions chronos
        if sys.platform == "win32":
            r = subprocess.run(
                ["wmic", "process", "where", "CommandLine like '%chronos%'",
                 "get", "ProcessId,CommandLine", "/format:csv"],
                capture_output=True, text=True, timeout=5)
            for ln in r.stdout.splitlines():
                parts = ln.strip().split(",")
                if len(parts) >= 3 and parts[-1].isdigit():
                    procs.append((parts[-1], ",".join(parts[1:-1])))
        else:
            r = subprocess.run(["pgrep", "-af", "chronos"],
                               capture_output=True, text=True, timeout=5)
            for ln in r.stdout.splitlines():
                pid, _, cmd = ln.partition(" ")
                if pid.isdigit():
                    procs.append((pid, cmd))
        procs = [(p, c) for p, c in procs if int(p) != me]
        if procs:
            # Daemon vs. anything else: name it honestly rather than defaulting
            # to "daemon" for every process that happens to be running.
            named = [f"{c.strip() or 'chronos'} (PID {p})" for p, c in procs]
            return f"  Held by: {'; '.join(named)}.\n"
    except Exception:
        pass
    return "  Holder unknown -- could not identify the process holding the lock.\n"


async def ensure_schema(driver):
    try:
        await driver.build_indices_and_constraints()
    except Exception:
        pass  # already built; kuzu raises on re-create
