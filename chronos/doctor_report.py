"""Structured, exit-code-bearing health checks for `chronos doctor`.

Separate from the existing prose diagnostics in cli.py::do_doctor (which stay
as-is) -- this is a fixed set of ok/warn/error checks with one line each, a
--json form, and an exit code CI can gate on. Each check is isolated: one
throwing never stops the others, and nothing here ever hangs (every I/O call
is bounded).
"""

import os
import time
from pathlib import Path

OK, WARN, ERROR = "ok", "warn", "error"
_LABEL_WIDTH = 16


def _symbols() -> dict:
    # A Windows console defaults to cp1252, which cannot encode these glyphs --
    # printing them raises UnicodeEncodeError and takes the whole command down
    # (same failure mode do_init works around for tick/warn/cross elsewhere).
    import sys
    try:
        "✓~✗".encode(sys.stdout.encoding or "utf-8")
        return {OK: "✓", WARN: "~", ERROR: "✗"}
    except (UnicodeEncodeError, LookupError):
        return {OK: "[ok]", WARN: "[~]", ERROR: "[x]"}


def _row(status: str, check: str, detail: str) -> dict:
    return {"status": status, "check": check, "detail": detail}


def _repo() -> Path:
    return Path(os.environ.get("CHRONOS_REPO_PATH") or ".").resolve()


def _check_db() -> dict:
    from . import db
    p = db.db_path()
    if not p.exists():
        return _row(ERROR, "chronos.db", f"not found at {p} — run: chronos init")
    if not os.access(p, os.R_OK):
        return _row(ERROR, "chronos.db", f"not readable at {p}")
    return _row(OK, "chronos.db", str(p))


async def _check_graph() -> dict:
    import asyncio
    from .store import GraphLocked, ensure_schema, open_driver
    from . import query, groups

    try:
        drv = open_driver()
        try:
            await ensure_schema(drv)
            grp = groups.resolve(None, str(_repo()))
            h = await query.health(drv, grp)
        finally:
            await drv.close()
    except GraphLocked:
        return _row(WARN, "graph store", "locked by another process (daemon holds it)")
    except (asyncio.TimeoutError, TimeoutError):
        return _row(ERROR, "graph store", "unreachable — check CHRONOS_DB path (timed out)")
    except Exception as e:
        return _row(ERROR, "graph store", f"unreachable — check CHRONOS_DB path ({type(e).__name__}: {e})")

    last = h.get("last_sync")
    if not last:
        return _row(WARN, "graph store", "cold (never indexed)")
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(last)
        age_h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        return _row(OK, "graph store", f"warm (last indexed: {age_h:.1f}h ago)")
    except ValueError:
        return _row(OK, "graph store", f"warm (last indexed: {last})")


def _check_precommit_hook() -> dict:
    hook = _repo() / ".git" / "hooks" / "pre-commit"
    if not hook.is_file():
        return _row(ERROR, "pre-commit hook", "not installed — run: chronos init")
    try:
        text = hook.read_text(encoding="utf-8")
    except OSError as e:
        return _row(ERROR, "pre-commit hook", f"unreadable ({type(e).__name__})")
    if "# chronos-managed" not in text:
        return _row(WARN, "pre-commit hook",
                    "hook present but not managed by Chronos — add: chronos precommit run")
    executable = os.access(hook, os.X_OK) if os.name != "nt" else True
    if not executable:
        return _row(WARN, "pre-commit hook", "installed but not executable")
    return _row(OK, "pre-commit hook", str(hook))


def _check_audit_log() -> dict:
    from . import db
    log = db.db_path().parent / "chronos-audit.log"
    if not log.exists():
        return _row(WARN, "audit log", "no entries yet (will be created on first tool call)")
    try:
        n = 0
        last_line = None
        with open(log, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    n += 1
                    last_line = line
    except OSError as e:
        return _row(WARN, "audit log", f"unreadable ({type(e).__name__})")
    if n == 0 or last_line is None:
        return _row(WARN, "audit log", "no entries yet (will be created on first tool call)")
    import json as _json
    ts = "unknown"
    try:
        ts = _json.loads(last_line).get("timestamp", "unknown")
    except ValueError:
        pass
    return _row(OK, "audit log", f"{n} entries, last: {ts}")


def _check_auth() -> dict:
    mode = os.environ.get("CHRONOS_AUTH", "")
    if mode == "strict":
        return _row(OK, "CHRONOS_AUTH", "strict mode (all calls require a valid API key)")
    return _row(ERROR, "CHRONOS_AUTH",
               "not set — running in permissive mode (any caller can use any tool)")


def _check_agents() -> dict:
    from . import db as _db
    try:
        con = _db.connect()
    except Exception as e:
        return _row(WARN, "agents", f"could not open chronos.db ({type(e).__name__})")
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "agents" not in tables:
            return _row(WARN, "agents", "table not found — F1 (identity) not initialised")
        active = con.execute("SELECT count(*) FROM agents WHERE status='active'").fetchone()[0]
        suspended = con.execute("SELECT count(*) FROM agents WHERE status='suspended'").fetchone()[0]
    finally:
        con.close()
    detail = f"{active} active, {suspended} suspended"
    if active == 0 and os.environ.get("CHRONOS_AUTH") == "strict":
        return _row(ERROR, "agents",
                   f"strict mode is on but no agents are registered — all calls will be rejected")
    return _row(OK if active > 0 else WARN, "agents", detail)


def _check_anomaly() -> dict:
    from . import db as _db
    try:
        con = _db.connect()
    except Exception as e:
        return _row(WARN, "anomaly", f"could not open chronos.db ({type(e).__name__})")
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "agent_baselines" not in tables:
            return _row(WARN, "anomaly", "table not found — F6 (anomaly detection) not initialised")
        with_baseline = con.execute("SELECT count(*) FROM agent_baselines").fetchone()[0]
        if "agents" in tables:
            total_agents = con.execute("SELECT count(*) FROM agents WHERE status='active'").fetchone()[0]
        else:
            total_agents = with_baseline
        learning = max(total_agents - with_baseline, 0)
    finally:
        con.close()
    detail = f"{with_baseline} agents with baselines, {learning} in learning mode (< 7 days history)"
    return _row(WARN if learning else OK, "anomaly", detail)


def _check_watcher() -> dict:
    from . import db as _db
    pid_file = _db.db_path().parent / "chronos-watch.pid"
    if not pid_file.is_file():
        return _row(WARN, "watcher",
                   "not running — F6/F7 coverage limited to MCP-routed calls (start with: chronos watch)")
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return _row(WARN, "watcher", "pid file unreadable/invalid")
    if _pid_alive(pid):
        return _row(OK, "watcher", f"running (pid {pid}) — F7 filesystem coverage active")
    return _row(WARN, "watcher",
               "not running — F6/F7 coverage limited to MCP-routed calls (start with: chronos watch)")


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except ProcessLookupError:
        return False


def _check_enforcement_rules() -> dict:
    from . import rule_store
    try:
        c = rule_store.counts()
    except Exception as e:
        return _row(WARN, "enforcement rules", f"could not read rule store ({type(e).__name__})")
    blocking = c.get("blocking", 0)
    warn_only = c.get("warn_only", 0)
    if blocking == 0 and warn_only == 0:
        return _row(WARN, "enforcement rules", "no rules defined — run: chronos generate-rule")
    return _row(OK, "enforcement rules", f"{blocking} blocking, {warn_only} warn-only")


_CHECKS = [
    _check_db,
    _check_graph,
    _check_precommit_hook,
    _check_audit_log,
    _check_auth,
    _check_agents,
    _check_anomaly,
    _check_watcher,
    _check_enforcement_rules,
]


async def run_checks() -> list[dict]:
    """Run every check in order, isolated: one exception never stops the rest."""
    import asyncio
    rows = []
    for fn in _CHECKS:
        label = fn.__name__.removeprefix("_check_").replace("_", " ")
        try:
            res = fn()
            if asyncio.iscoroutine(res):
                res = await res
            rows.append(res)
        except Exception as e:
            rows.append(_row(ERROR, label, f"check failed: {type(e).__name__}: {e}"))
    return rows


def render(rows: list[dict]) -> str:
    symbols = _symbols()
    lines = []
    for r in rows:
        label = f"{r['check']}:".ljust(_LABEL_WIDTH)
        lines.append(f"{symbols[r['status']]} {label} {r['detail']}")
    return "\n".join(lines)


def exit_code(rows: list[dict]) -> int:
    return 1 if any(r["status"] == ERROR for r in rows) else 0


def run(as_json: bool = False) -> int:
    import asyncio
    rows = asyncio.run(run_checks())
    if as_json:
        import json
        print(json.dumps(rows, indent=2))
    else:
        print(render(rows))
    return exit_code(rows)
