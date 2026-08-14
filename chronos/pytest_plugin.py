"""Automatic failure capture for Wedge 2, as a pytest plugin.

Why a plugin and not a PostToolUse hook: a Bash command that exits non-zero
does not fire PostToolUse (verified empirically 2026-08-14 — three failing
commands, none delivered to the hook; ten successful ones, all delivered).
The hook can therefore never see a failing test run, which is the single
highest-signal event Wedge 2 wants. pytest owns its own exit status, so it
sees every failure by construction.

Contract, same as triggers.py: capture is BEST-EFFORT and must never affect
the test run. Every write is wrapped; a broken Chronos install costs you a
line on stderr, never a red build.

The plugin only writes a trace file. Nothing here calls an LLM or touches the
network — trace_processor.py does that later, out of band.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TRACES_DIR_NAME = ".chronos/traces"
MAX_TRACEBACK_CHARS = 3000   # cap per failure — avoid huge pending.jsonl entries
MAX_FAILURES = 50            # a 2000-test wipeout is one bug, not 2000 lessons


class ChronosCapturePlugin:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.traces_dir = repo_path / TRACES_DIR_NAME
        self.failures: list[dict] = []
        self.session_start = datetime.now(timezone.utc).isoformat()

    def pytest_runtest_logreport(self, report):
        """Capture each individual test failure."""
        if report.when == "call" and report.failed:
            if len(self.failures) >= MAX_FAILURES:
                return
            tb = ""
            if report.longrepr:
                tb = str(report.longrepr)[-MAX_TRACEBACK_CHARS:]
            self.failures.append({
                "test_id": report.nodeid,
                "outcome": "failed",
                "message": _extract_message(report),
                "traceback": tb,
                "duration_s": round(getattr(report, "duration", 0), 3),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            })

    def pytest_sessionfinish(self, session, exitstatus):
        """Write session trace to pending.jsonl after the run completes."""
        if not self.failures:
            return   # nothing to learn from a clean run
        trace = {
            "source": "pytest",
            "session_start": self.session_start,
            "session_end": datetime.now(timezone.utc).isoformat(),
            "exit_status": int(exitstatus),
            "total_failed": len(self.failures),
            "failures": self.failures,
            "repo_path": str(self.repo_path),
            "cwd": str(Path.cwd()),
        }
        self._append_pending(trace)

    def _append_pending(self, trace: dict):
        try:
            self.traces_dir.mkdir(parents=True, exist_ok=True)
            pending = self.traces_dir / "pending.jsonl"
            with pending.open("a", encoding="utf-8") as f:
                f.write(json.dumps(trace) + "\n")
        except Exception as e:  # noqa: BLE001 -- best-effort by contract
            # Never let capture failure break the test run.
            print(f"[Chronos] trace write failed: {e}", file=sys.stderr)


def _extract_message(report) -> str:
    """Pull the assertion message from longrepr without the full traceback."""
    if not report.longrepr:
        return ""
    text = str(report.longrepr)
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    # pytest marks the failing expression's lines with a leading "E". Those
    # carry the assertion message; the final line is a "path:lineno: ExcType"
    # footer, which names the exception but drops the message that explains it.
    for line in lines:
        if line.startswith("E ") or line == "E":
            msg = line[1:].strip()
            if msg:
                return msg[:500]
    for line in reversed(lines):
        if not line.startswith(("_", "=", "-")):
            return line[:500]
    return lines[-1][:500] if lines else ""


def _find_repo_root(start: Path) -> Path:
    """Walk up from start looking for .chronos/ or .git/."""
    for parent in [start, *start.parents]:
        if (parent / ".chronos").is_dir() or (parent / ".git").is_dir():
            return parent
    return start   # fallback: wherever pytest was invoked


def pytest_configure(config):
    """Entry point — registered via entry_points (pytest11) or conftest."""
    # Opt-out for anyone who wants pytest untouched.
    if os.environ.get("CHRONOS_CAPTURE", "").strip().lower() in ("0", "false", "no", "off"):
        return
    # Guard against double registration. The pytest11 entry point and a
    # conftest carrying `pytest_plugins = ["chronos.pytest_plugin"]` can both
    # fire; without this the second one attaches a second capture plugin and
    # every failure is written to pending.jsonl twice.
    #
    # NOTE: this cannot rescue the conftest case on its own -- pluggy rejects
    # the duplicate MODULE before any code here runs ("Plugin already
    # registered under a different name"), which aborts the whole session. With
    # chronos installed, the entry point is sufficient and the conftest line
    # must be removed; see this repo's conftest.py.
    if config.pluginmanager.hasplugin("chronos_capture"):
        return
    try:
        repo_root = _find_repo_root(Path(str(config.rootpath)))
        chronos_sqlite = os.environ.get("CHRONOS_SQLITE", "")
        if chronos_sqlite:
            # Derive repo root from CHRONOS_SQLITE, but ONLY when it refers to
            # the tree being tested. A stale var inherited from another repo
            # (or another test) would otherwise divert traces somewhere the
            # developer will never look — silently losing every lesson.
            db_path = Path(chronos_sqlite)
            if db_path.parent.name == ".chronos":
                candidate = db_path.parent.parent
                try:
                    same_tree = (candidate == repo_root
                                 or candidate.resolve() in repo_root.resolve().parents
                                 or repo_root.resolve() in candidate.resolve().parents)
                except OSError:
                    same_tree = False
                if same_tree:
                    repo_root = candidate
        plugin = ChronosCapturePlugin(repo_root)
        config.pluginmanager.register(plugin, "chronos_capture")
    except Exception as e:  # noqa: BLE001
        # A plugin that cannot register must not abort collection.
        print(f"[Chronos] capture plugin disabled: {e}", file=sys.stderr)
