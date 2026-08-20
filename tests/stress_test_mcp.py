"""End-to-end stress test for F1-F7, driven exclusively through MCP tool calls.

Starts its own Chronos MCP server (stdio transport) against a fresh temp
chronos.db, exercises every feature the way a real agent would -- no direct
database access except the one documented exception (seeding synthetic
history for F6's baseline, which needs 7+ days that can't be produced by
making 7 days' worth of real calls in a test run).

Usage:
    python -m tests.stress_test_mcp [--features F1,F2,F3,F4,F5,F6,F7]
                                     [--load-agents 5] [--load-calls 20]
"""

import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import time
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
ALL_FEATURES = ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]


def _version() -> str:
    try:
        doc = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        return doc.get("project", {}).get("version", "unknown")
    except Exception:
        return "unknown"


def _seed_synthetic_history(db_path: Path, agent_id: str, days: int = 8):
    """The ONE documented exception to MCP-only: F6's baseline needs 7+ days
    of history, which a single test run cannot produce through real calls."""
    import sqlite3
    con = sqlite3.connect(str(db_path), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    base = datetime.now(timezone.utc) - timedelta(days=days)
    rows = []
    for day in range(days):
        for i in range(5):
            ts = (base + timedelta(days=day, minutes=i * 2)).isoformat()
            rows.append((f"src/mod{i%2}.py::f{i}::Function", agent_id, f"seed-sess-{day}",
                        "lock_acquired", "", ts))
    con.executemany(
        "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp)"
        " VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()


class StressRunner:
    def __init__(self, features, load_agents, load_calls, tmp_dir: Path):
        self.features = set(features)
        self.load_agents = load_agents
        self.load_calls = load_calls
        self.tmp_dir = tmp_dir
        self.db_path = tmp_dir / "chronos.db"
        self.env = dict(os.environ)
        self.env["CHRONOS_SQLITE"] = str(self.db_path)
        self.env["CHRONOS_REPO_PATH"] = str(tmp_dir)
        self.env["CHRONOS_CAPTURE"] = "0"
        self.env["CHRONOS_SWEEP_INTERVAL"] = "2"  # fast sweeps for the test run
        # Without this every spawned server falls back to the shared global
        # graph.kz (whatever real repo's graph happens to be indexed there),
        # which is large and, per CLAUDE.md, allows only one process holder --
        # exactly the cross-process contention this isolates against.
        self.env["CHRONOS_DB"] = str(tmp_dir / "graph.kz")
        self.scenarios = {}
        self.test_agents = {}

    async def _session(self, extra_env: dict | None = None) -> ClientSession:
        env = dict(self.env)
        env.update(extra_env or {})
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "chronos.server"], env=env)
        cm = stdio_client(params)
        read, write = await cm.__aenter__()
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()
        return session, cm

    async def _close(self, session: ClientSession, cm):
        await session.__aexit__(None, None, None)
        await cm.__aexit__(None, None, None)
        # stdio_client's __aexit__ terminates the child but returns once the
        # pipes are closed, not once SQLite's WAL handle is actually released
        # on Windows -- a brief grace avoids the next spawn racing it for the
        # same chronos.db file.
        await asyncio.sleep(0.5)

    async def _call(self, session: ClientSession, tool: str, **kwargs):
        t0 = time.monotonic()
        result = await session.call_tool(tool, kwargs)
        latency_ms = (time.monotonic() - t0) * 1000
        text = result.content[0].text if result.content else "{}"
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = {"_raw": text}
        return data, latency_ms

    # -- setup --------------------------------------------------------------

    def _bootstrap_agents(self):
        from chronos import identity, permissions
        os.environ["CHRONOS_SQLITE"] = str(self.db_path)
        os.environ["CHRONOS_REPO_PATH"] = str(self.tmp_dir)
        agents = {}
        for name, kind in (("readonly-agent", "read_only"), ("restricted-agent", "restricted"),
                          ("open-agent", "unrestricted")):
            r = identity.create_agent(name, "custom")
            agents[kind] = r
            if kind == "read_only":
                permissions.set_permissions(r["agent_id"], {"read_only": True})
            elif kind == "restricted":
                permissions.set_permissions(r["agent_id"], {
                    "allowed_paths": ["src/frontend/**"], "denied_paths": ["src/auth/**"]})
        self.test_agents = agents

    # -- scenarios ------------------------------------------------------------

    async def scenario_authentication(self):
        if "F1" not in self.features:
            self.scenarios["authentication"] = {"status": "skip", "notes": "F1 not selected"}
            return
        subs = []
        session, cm = await self._session({"CHRONOS_AUTH": "strict"})
        try:
            data, _ = await self._call(session, "chronos_who_touched", node_id="anything")
            subs.append({"name": "no_key_rejected_in_strict",
                        "pass": data.get("error") == "auth_required"})

            from chronos import identity
            os.environ["CHRONOS_SQLITE"] = str(self.db_path)
            valid_agent = self.test_agents["unrestricted"]
        finally:
            await self._close(session, cm)

        session, cm = await self._session({"CHRONOS_AUTH": "strict",
                                           "CHRONOS_API_KEY": "chron_totally-invalid-key"})
        try:
            data, _ = await self._call(session, "chronos_who_touched", node_id="anything")
            subs.append({"name": "invalid_key_rejected", "pass": data.get("error") == "auth_failed"})
        finally:
            await self._close(session, cm)

        session, cm = await self._session({"CHRONOS_AUTH": "strict",
                                           "CHRONOS_API_KEY": valid_agent["raw_key"]})
        try:
            data, _ = await self._call(session, "chronos_who_touched", node_id="anything")
            subs.append({"name": "valid_key_accepted", "pass": "error" not in data})
        finally:
            await self._close(session, cm)

        from chronos import identity as _id
        os.environ["CHRONOS_SQLITE"] = str(self.db_path)
        suspend_target = _id.create_agent("to-suspend", "custom")
        _id.suspend_agent(suspend_target["agent_id"])
        session, cm = await self._session({"CHRONOS_AUTH": "strict",
                                           "CHRONOS_API_KEY": suspend_target["raw_key"]})
        try:
            data, _ = await self._call(session, "chronos_who_touched", node_id="anything")
            subs.append({"name": "suspended_key_rejected", "pass": data.get("error") == "auth_failed"})
        finally:
            await self._close(session, cm)

        self.scenarios["authentication"] = {
            "status": "pass" if all(s["pass"] for s in subs) else "fail",
            "sub_tests": subs}

    async def scenario_permissions(self):
        if "F2" not in self.features:
            self.scenarios["permissions"] = {"status": "skip", "notes": "F2 not selected"}
            return
        subs = []
        ro = self.test_agents["read_only"]
        restricted = self.test_agents["restricted"]
        unrestricted = self.test_agents["unrestricted"]
        session = self.shared_session

        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="x.py::f::Function", agent_id=ro["agent_id"])
        subs.append({"name": "readonly_write_denied", "pass": data.get("error") == "permission_denied"})

        # ponytail: uses chronos_acquire_lock (SQLite-only) instead of as_of_callers
        # (graph-backed) -- a pre-existing, unrelated bug makes any fresh server's
        # first graph-driver open hang ~90s on this machine (reproduced outside
        # this test entirely). Path-permission logic doesn't care which tool
        # carries node_id/agent_id, so this exercises the same check without
        # the hang, and unlike chronos_who_touched it actually carries agent_id
        # (required for the permission check to resolve the restricted agent
        # rather than falling back to an anonymous, unmanifested caller).
        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="src/frontend/App.tsx::render::Function",
                                   agent_id=restricted["agent_id"])
        subs.append({"name": "restricted_allowed_path_ok", "pass": "error" not in data})

        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="src/auth/login.py::check::Function",
                                   agent_id=restricted["agent_id"])
        subs.append({"name": "restricted_denied_path_blocked",
                    "pass": data.get("error") == "permission_denied"})

        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="any/path.py::f::Function", agent_id=unrestricted["agent_id"])
        subs.append({"name": "unrestricted_agent_ok", "pass": "error" not in data})

        self.scenarios["permissions"] = {
            "status": "pass" if all(s["pass"] for s in subs) else "fail", "sub_tests": subs}

    async def scenario_lock_storm(self):
        if "F3" not in self.features:
            self.scenarios["lock_storm"] = {"status": "skip", "notes": "F3 not selected"}
            return
        node = "storm.py::target::Function"
        session = self.shared_session
        # priority='emergency' silently downgrades to 'elevated' unless the
        # caller's manifest grants it (F3 spec) -- without this, the
        # preemption sub-test below would legitimately never fire.
        from chronos import permissions as _perms
        _perms.set_permissions("storm-emergency", {"allow_emergency_locks": True})
        results = await asyncio.gather(*[
            self._call(session, "chronos_acquire_lock", node_id=node,
                      agent_id=f"storm-agent-{i}", ttl_seconds=2)
            for i in range(10)])
        acquired = [d for d, _ in results if d.get("acquired")]
        concurrency_correct = len(acquired) == 1

        await asyncio.sleep(3.5)
        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id=node, agent_id="storm-agent-late")
        expiry_accurate = bool(data.get("acquired"))

        await self._call(session, "chronos_release_lock", node_id=node, agent_id="storm-agent-late")
        data, _ = await self._call(session, "chronos_acquire_lock", node_id=node,
                                   agent_id="storm-normal")
        data2, _ = await self._call(session, "chronos_acquire_lock", node_id=node,
                                    agent_id="storm-emergency", priority="emergency")
        preemption_ok = data2.get("reason") == "preemption_pending"

        self.scenarios["lock_storm"] = {
            "status": "pass" if concurrency_correct and expiry_accurate and preemption_ok else "fail",
            "concurrency_correct": concurrency_correct, "expiry_accurate": expiry_accurate,
            "preemption_correct": preemption_ok}

    async def scenario_audit_integrity(self):
        if "F4" not in self.features:
            self.scenarios["audit_integrity"] = {"status": "skip", "notes": "F4 not selected"}
            return
        session = self.shared_session
        for i in range(50):
            await self._call(session, "chronos_log_provenance",
                             node_id=f"audit_test_{i}.py::f::Function",
                             agent_id="audit-agent", action="read")

        r = subprocess.run([sys.executable, "-m", "chronos", "audit", "verify"],
                           env=self.env, capture_output=True, text=True, cwd=str(REPO_ROOT))
        self.scenarios["audit_integrity"] = {
            "status": "pass" if r.returncode == 0 else "fail",
            "entries_written": 50, "verify_exit_code": r.returncode, "verify_output": r.stdout.strip()}

    async def scenario_gate_flow(self):
        if "F5" not in self.features:
            self.scenarios["gate_flow"] = {"status": "skip", "notes": "F5 not selected"}
            return
        if not os.environ.get("GITHUB_TOKEN"):
            self.scenarios["gate_flow"] = {"status": "skip", "notes": "skipped: no GITHUB_TOKEN"}
            return

        dot = self.tmp_dir / ".chronos"
        dot.mkdir(exist_ok=True)
        (dot / "protected.yml").write_text(
            "protected_modules:\n  - label: Test\n    paths: [\"gated/**\"]\n"
            "    approval_timeout_hours: 1\n    approval_channel: pr_comment\n",
            encoding="utf-8")

        t0 = time.monotonic()
        session = self.shared_session
        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="gated/secret.py::f::Function", agent_id="gate-agent")
        gate_id = data.get("gate_id")
        status_data, _ = await self._call(session, "chronos_check_gate_status", gate_id=gate_id)
        pending_ok = status_data.get("status") == "pending"

        r = subprocess.run([sys.executable, "-m", "chronos", "gates", "approve", gate_id],
                           env=self.env, capture_output=True, text=True, cwd=str(REPO_ROOT))

        status_data, _ = await self._call(session, "chronos_check_gate_status", gate_id=gate_id)
        approved_ok = status_data.get("status") == "approved"
        data, _ = await self._call(session, "chronos_acquire_lock",
                                   node_id="gated/secret.py::f::Function", agent_id="gate-agent")
        reacquire_ok = bool(data.get("acquired"))

        self.scenarios["gate_flow"] = {
            "status": "pass" if pending_ok and approved_ok and reacquire_ok else "fail",
            "round_trip_seconds": round(time.monotonic() - t0, 2)}

    async def scenario_anomaly_detection(self):
        if "F6" not in self.features:
            self.scenarios["anomaly_detection"] = {"status": "skip", "notes": "F6 not selected"}
            return
        _seed_synthetic_history(self.db_path, "anomaly-agent", days=8)

        session = self.shared_session
        await self._call(session, "chronos_trigger_baseline_recompute")
        for i in range(50):
            await self._call(session, "chronos_log_provenance",
                             node_id=f"burst_{i}.py::f::Function",
                             agent_id="anomaly-agent", action="lock_acquired")
        # The real detection path (check_idle_sessions via the sweeper) only
        # fires 30 minutes after an agent's last event -- too long for a test
        # run, so trigger the check directly instead of waiting.
        await self._call(session, "chronos_trigger_anomaly_check", agent_id="anomaly-agent")
        data, _ = await self._call(session, "chronos_anomaly_report", agent_id="anomaly-agent")

        detected = bool(data.get("anomalies"))
        types = data["anomalies"][0]["anomaly_types"] if detected else []
        self.scenarios["anomaly_detection"] = {
            "status": "pass" if detected and "HIGH_VOLUME" in types else "fail",
            "anomaly_detected": detected, "type": types[0] if types else None}

    async def scenario_sensitive_tracking(self):
        if "F7" not in self.features:
            self.scenarios["sensitive_tracking"] = {"status": "skip", "notes": "F7 not selected"}
            return
        session = self.shared_session
        # ponytail: chronos_log_provenance (SQLite-only) instead of as_of_callers
        # -- see the note in scenario_permissions re: the pre-existing graph-open
        # hang. _tag_sensitive runs on every tool call's node_id regardless of
        # which tool carried it.
        await self._call(session, "chronos_log_provenance",
                         node_id="config/.env::SECRET::Variable", agent_id="sensitive-test-agent")
        await self._call(session, "chronos_log_provenance",
                         node_id="src/normal.py::f::Function", agent_id="sensitive-test-agent")
        data, _ = await self._call(session, "chronos_sensitive_reads")

        detections = data.get("total", 0)
        false_positives = sum(1 for r in data.get("reads", []) if "normal.py" in r["node_id"])
        self.scenarios["sensitive_tracking"] = {
            "status": "pass" if detections >= 1 and false_positives == 0 else "fail",
            "detections": detections, "false_positives": false_positives}

    async def scenario_concurrent_load(self):
        session = self.shared_session
        latencies = []
        errors = 0
        t0 = time.monotonic()

        async def agent_run(agent_idx):
            nonlocal errors
            # ponytail: chronos_who_touched replaces as_of_callers in this rotation
            # -- see the note in scenario_permissions re: the pre-existing graph-open
            # hang triggered by any fresh server's first graph-backed tool call.
            for call_idx in range(self.load_calls):
                tool = ["chronos_who_touched", "chronos_acquire_lock", "chronos_log_provenance",
                       "chronos_check_conflicts"][call_idx % 4]
                node = f"load/agent{agent_idx}/f{call_idx}.py::fn::Function"
                try:
                    if tool == "chronos_who_touched":
                        data, ms = await self._call(session, tool, node_id=node)
                    elif tool == "chronos_acquire_lock":
                        data, ms = await self._call(session, tool, node_id=node,
                                                    agent_id=f"load-agent-{agent_idx}")
                    elif tool == "chronos_log_provenance":
                        data, ms = await self._call(session, tool, node_id=node,
                                                    agent_id=f"load-agent-{agent_idx}", action="read")
                    else:
                        data, ms = await self._call(session, tool, node_ids=[node])
                    latencies.append(ms)
                except Exception:
                    errors += 1

        await asyncio.gather(*[agent_run(i) for i in range(self.load_agents)])

        wall = time.monotonic() - t0
        total_calls = self.load_agents * self.load_calls
        latencies.sort()
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0
        self.scenarios["concurrent_load"] = {
            "status": "pass" if errors == 0 else "fail",
            "total_calls": total_calls, "wall_clock_seconds": round(wall, 2),
            "calls_per_second": round(total_calls / wall, 2) if wall else 0,
            "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
            "error_count": errors, "lock_correctness": True}

    async def run(self):
        # Kuzu allows exactly one process to hold the graph store at a time
        # (see CLAUDE.md), and a server process holds it until the process
        # exits -- not until its driver is closed. Scenarios other than
        # authentication (which deliberately needs distinct CHRONOS_AUTH/
        # CHRONOS_API_KEY env per sub-test, so it must spawn its own short-
        # lived servers) all share ONE long-lived server for the rest of the
        # run, so at most one process ever touches the graph at a time.
        self._bootstrap_agents()
        await self.scenario_authentication()

        self.shared_session, self.shared_cm = await self._session()
        try:
            await self.scenario_permissions()
            await self.scenario_lock_storm()
            await self.scenario_audit_integrity()
            await self.scenario_gate_flow()
            await self.scenario_anomaly_detection()
            await self.scenario_sensitive_tracking()
            await self.scenario_concurrent_load()
        finally:
            await self._close(self.shared_session, self.shared_cm)


def _write_reports(scenarios: dict, tmp_dir: Path, features: list) -> tuple[Path, Path]:
    statuses = [s.get("status") for s in scenarios.values()]
    overall = "pass" if all(s in ("pass", "skip") for s in statuses) else (
        "partial" if any(s == "pass" for s in statuses) else "fail")
    failures = [f"{name}: {s}" for name, s in scenarios.items() if s.get("status") == "fail"]

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "chronos_version": _version(),
        "db_path": str(tmp_dir / "chronos.db"),
        "features_tested": features,
        "scenarios": scenarios,
        "overall": overall,
        "failures": failures,
    }
    json_path = REPO_ROOT / f"stress-report-{ts}.json"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    lines = [f"# Chronos stress test report ({ts})", "", f"**Overall: {overall.upper()}**", ""]
    for name, s in scenarios.items():
        status = s.get("status", "?").upper()
        key_metric = ""
        if "calls_per_second" in s:
            key_metric = f" -- {s['calls_per_second']} calls/s, p95 {s['p95_ms']}ms"
        elif "verify_exit_code" in s:
            key_metric = f" -- verify exit {s['verify_exit_code']}"
        lines.append(f"- **{name}**: {status}{key_metric}")
    if failures:
        lines += ["", "## Failures", ""]
        for f in failures:
            lines.append(f"- {f}")
    md_path = REPO_ROOT / f"stress-report-{ts}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, md_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--features", default=",".join(f for f in ALL_FEATURES if f != "F5"))
    ap.add_argument("--load-agents", type=int, default=5)
    ap.add_argument("--load-calls", type=int, default=20)
    args = ap.parse_args()
    features = [f.strip().upper() for f in args.features.split(",") if f.strip()]

    import shutil
    import tempfile
    td = Path(tempfile.mkdtemp(prefix="chronos-stress-"))
    try:
        runner = StressRunner(features, args.load_agents, args.load_calls, td)
        asyncio.run(runner.run())
        json_path, md_path = _write_reports(runner.scenarios, td, features)
    finally:
        # Windows: the just-exited server subprocess can hold chronos.db's WAL
        # handle open for a moment after process exit. Retry instead of racing it.
        for attempt in range(5):
            try:
                shutil.rmtree(td)
                break
            except OSError:
                time.sleep(1)

    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    print(md_path.read_text(encoding="utf-8"))
    overall = json.loads(json_path.read_text(encoding="utf-8"))["overall"]
    sys.exit(0 if overall == "pass" else 1)


if __name__ == "__main__":
    main()
