"""Drain .chronos/traces/pending.jsonl into the Reflector.

The pytest plugin writes traces synchronously and cheaply; this is where the
expensive half happens, out of band. Splitting it that way is the point: a
test run must never pay for an LLM round-trip (triggers.py measured 5,099ms
vs 134ms when Trigger 1 was inline — a 38x tax).

Best-effort by the same contract as triggers.py. Nothing here may raise into
a caller: this runs on MCP server startup and inside `chronos enforce`, and
neither may fail because a trace was malformed.
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from .pytest_plugin import _find_repo_root

log = logging.getLogger("chronos.trace_processor")

MAX_AGE_HOURS = 24
FAILURE_PATTERNS = ("FAILED", "ERROR", "AssertionError", "error:", "Traceback")


def _pending_path(repo_path) -> Path:
    return Path(repo_path) / ".chronos" / "traces" / "pending.jsonl"


def _age_hours(iso_ts: str) -> float:
    """Hours since iso_ts. Unparseable -> 0.0, i.e. treat as fresh.

    Deliberate: discarding a trace we cannot date would silently lose a lesson,
    which is the one outcome this system exists to prevent."""
    if not iso_ts:
        return 0.0
    try:
        ts = datetime.fromisoformat(iso_ts)
    except (ValueError, TypeError):
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def _is_high_signal(trace: dict) -> bool:
    """A trace is high-signal if it contains real failures."""
    if trace.get("source") == "pytest":
        return int(trace.get("total_failed") or 0) > 0
    # PostToolUse hook traces (exit-0 runs whose output still names failures)
    blob = f"{trace.get('stdout', '')}\n{trace.get('stderr', '')}"
    return any(p in blob for p in FAILURE_PATTERNS)


def to_reflector_trace(trace: dict) -> dict:
    """pytest/hook trace -> the dict reflector.reflect() expects.

    The Reflector's shape is fixed by reflector.py and triggers.py:
    {agent_id, session_id, action, outcome, error_or_correction,
     nodes_touched, timestamp}.

    nodes_touched stays empty: a test id is not a graph qualified_name, and
    inventing one would produce decorated rules rather than grounded ones.
    reflector.ground() handles the empty case explicitly ("no nodes named in
    trace"), so an ungrounded lesson is honestly labelled as such."""
    if trace.get("source") == "pytest":
        failures = trace.get("failures") or []
        first = failures[0] if failures else {}
        names = ", ".join(f.get("test_id", "") for f in failures[:5])
        detail = "\n\n".join(
            f"{f.get('test_id', '')}: {f.get('message', '')}\n{f.get('traceback', '')}"
            for f in failures[:3]
        )
        return {
            "agent_id": trace.get("agent_id") or "pytest",
            "session_id": trace.get("session_start") or "pytest",
            "action": f"ran the test suite in {trace.get('repo_path', '')}",
            "outcome": "failed",
            "error_or_correction": (
                f"{trace.get('total_failed', 0)} test(s) failed: {names}\n\n{detail}"
            ),
            "nodes_touched": [],
            "timestamp": trace.get("session_end") or first.get("captured_at", ""),
        }
    return {
        "agent_id": trace.get("agent_id") or "bash-hook",
        "session_id": trace.get("session_id") or "bash",
        "action": trace.get("command") or "ran a shell command",
        "outcome": "failed",
        "error_or_correction": f"{trace.get('stdout', '')}\n{trace.get('stderr', '')}"[:4000],
        "nodes_touched": [],
        "timestamp": trace.get("captured_at") or "",
    }


def _dispatch_safe(trace: dict):
    """Reflect + curate on a background thread. Swallows everything."""
    try:
        from . import triggers
        if not triggers.enabled():
            return None
        payload = to_reflector_trace(trace)
        # triggers._reflect owns the sync/async bridge; reuse it rather than
        # re-deriving the event-loop handling here.
        candidate = triggers._reflect(payload, None, os.environ.get(
            "CHRONOS_GROUP_ID", "default"))
        if candidate is None:
            log.info("trace-processor: reflector returned None (low-signal)")
            return None
        from . import curator
        return curator.curate(candidate)
    except Exception as e:  # noqa: BLE001 -- best-effort by contract
        log.warning("trace-processor: dispatch failed (%s: %s)", type(e).__name__, e)
        return None


def _dispatch_to_reflector(trace: dict):
    """Fire-and-forget to the Reflector — same pattern as Trigger 1."""
    t = threading.Thread(target=_dispatch_safe, args=(trace,), daemon=True,
                         name="chronos-trace-processor")
    t.start()
    return t


def process_pending(repo_path=None) -> int:
    """Process all pending traces. Returns count of traces dispatched.

    Called by: MCP server startup, chronos enforce."""
    try:
        if repo_path is None:
            repo_path = _find_repo_root(Path.cwd())
        pending = _pending_path(repo_path)
        if not pending.exists():
            return 0
        dispatched = 0
        kept = []
        for line in pending.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                trace = json.loads(line)
            except json.JSONDecodeError:
                continue   # malformed line: drop it, never crash the caller
            age = _age_hours(trace.get("session_end") or trace.get("captured_at", ""))
            if age > MAX_AGE_HOURS:
                continue   # discard stale traces
            if _is_high_signal(trace):
                _dispatch_to_reflector(trace)
                dispatched += 1
            else:
                kept.append(line)   # low-signal: keep until it ages out
        pending.write_text("\n".join(kept) + "\n" if kept else "", encoding="utf-8")
        return dispatched
    except Exception as e:  # noqa: BLE001
        log.warning("trace-processor: process_pending failed (%s: %s)",
                    type(e).__name__, e)
        return 0


def process_pending_async(repo_path=None):
    """Non-blocking process_pending, for server startup."""
    t = threading.Thread(target=process_pending, args=(repo_path,), daemon=True,
                         name="chronos-trace-startup")
    t.start()
    return t


if __name__ == "__main__":
    n = process_pending()
    print(f"[Chronos] {n} trace(s) dispatched to Reflector", file=sys.stderr)
