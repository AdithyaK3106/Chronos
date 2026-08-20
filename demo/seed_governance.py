"""Seed F1-F7 governance state into novapay's chronos.db, so the dashboard's
governance panels (agents, permissions, gates, anomalies, sensitive reads,
audit) have real data on first open instead of every empty state at once.

Uses the real identity/permissions/gates/anomaly functions -- the same code
the MCP tools call -- against novapay's own chronos.db, not synthetic rows.

ponytail: one-shot seed script, not idempotent (create_agent mints a new
agent_id and audit.append() is append-only by design). Re-run via
`make demo-fixture`, which starts from git's committed .chronos/ state, not
by running this file twice against the same db.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NOVAPAY = ROOT / "demo" / "novapay"
os.environ.setdefault("CHRONOS_SQLITE", str(NOVAPAY / ".chronos" / "chronos.db"))
os.environ.setdefault("CHRONOS_REPO_PATH", str(NOVAPAY))

from chronos import gates, identity, ledger, permissions  # noqa: E402


def main():
    con = ledger.connect()

    # F1/F2 -- three agents with distinct manifests, mirroring seed_ledger.py's
    # claude-code/cursor split plus one restricted third agent so the
    # permissions panel has something to actually show scoping on.
    cc = identity.create_agent("claude-code", "custom", owner="platform-eng",
                               description="primary coding agent, unrestricted")
    cursor = identity.create_agent("cursor", "custom", owner="platform-eng",
                                   description="secondary coding agent, unrestricted")
    intern = identity.create_agent("intern-bot", "custom", owner="platform-eng",
                                   description="review-only agent, no write access")
    permissions.set_permissions(intern["agent_id"], {
        "read_only": True,
        "allowed_paths": ["src/payments/**"],
    })
    print(f"agents: claude-code={cc['agent_id'][:8]}  cursor={cursor['agent_id'][:8]}  "
          f"intern-bot={intern['agent_id'][:8]} (read-only, src/payments/** only)")

    # F5 -- a pending gate on a protected module, so the "human approval
    # required" panel isn't empty on first open. .chronos/protected.yml
    # ships committed alongside chronos.db so `chronos gates approve` works
    # against this same fixture without extra setup.
    protected = NOVAPAY / ".chronos" / "protected.yml"
    protected.write_text(
        "protected_modules:\n"
        "  - label: Payment provider credentials\n"
        "    paths: [\"src/payments/secrets/**\"]\n"
        "    approval_timeout_hours: 24\n",
        encoding="utf-8")
    gate_module = gates.is_protected("src/payments/secrets/provider_keys.py")
    gate = gates.request_gate(
        "src/payments/secrets/provider_keys.py::rotate_key::Function",
        cursor["agent_id"], "session-gate-demo", gate_module, con)
    print(f"gate requested: {gate['gate_id']} on {gate_module['label']} "
          f"(approve with: chronos gates approve {gate['gate_id']})")

    # F6 -- 9 days of synthetic history for intern-bot with one clear outlier
    # session (10x its normal volume), so anomaly_detection has something to
    # find without the demo needing to run for a week first. This is the one
    # documented direct-SQLite exception (matches stress_test_mcp.py's
    # _seed_synthetic_history) -- a baseline genuinely needs 7+ days, which
    # can't come from real calls in a single seed run.
    base = datetime.now(timezone.utc) - timedelta(days=9)
    for day in range(8):
        for i in range(4):
            ts = base + timedelta(days=day, minutes=i * 3)
            con.execute(
                "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp)"
                " VALUES (?,?,?,?,?,?)",
                (f"src/payments/service.py::f{i}::Function", intern["agent_id"],
                 f"seed-sess-{day}", "queried", "", ts.isoformat()))
    outlier_day = base + timedelta(days=8)
    for i in range(40):  # ~10x the seeded 4-per-session baseline
        ts = outlier_day + timedelta(seconds=i * 20)
        con.execute(
            "INSERT INTO provenance_events (node_id, agent_id, session_id, action, reason, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            (f"src/payments/service.py::f{i%4}::Function", intern["agent_id"],
             "seed-sess-outlier", "queried", "", ts.isoformat()))
    con.commit()

    from chronos import anomaly
    n = anomaly.compute_baselines(con)
    rows = con.execute(
        "SELECT node_id, agent_id, session_id, action, timestamp FROM provenance_events "
        "WHERE agent_id=? ORDER BY timestamp", (intern["agent_id"],)).fetchall()
    sessions = anomaly._sessions_for(rows)
    result = anomaly.check_session(intern["agent_id"], sessions[-1], con)
    print(f"baselines computed for {n} agent(s); outlier session -> "
          f"{result['anomaly_types'] if result else 'no anomaly (unexpected)'}")

    # F7 -- a sensitive-path read, logged the same way _tag_sensitive does in
    # server.py (a provenance_events row with action='sensitive_read').
    ledger.log_event(con, "src/payments/secrets/.env::PROVIDER_API_KEY::Variable",
                     agent_id=cursor["agent_id"], session_id="session-gate-demo",
                     action="sensitive_read",
                     reason='{"label": "secrets", "severity": "high", "pattern_matched": ".env"}')

    con.commit()
    print("governance seed complete")


if __name__ == "__main__":
    main()
