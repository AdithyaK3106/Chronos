"""Seed 3 realistic agent sessions into novapay's intent/provenance ledger
(chronos/ledger.py), so the ledger/dashboard has data on first open instead
of an empty state. Per demo-spec.md Option 3:

  Session 1: query-only (no writes)
  Session 2: a write session using the correct patterns (all good)
  Session 3: a write session blocked by CI (no-requests fires)

Uses the real ledger.acquire/release/log_event functions -- the same calls
chronos_acquire_lock/chronos_release_lock/chronos_log_provenance make over
MCP -- against novapay's own chronos.db, not a synthetic table.

ponytail: one-shot seed script. Re-run is NOT idempotent (log_event is
append-only by design), so demo-reset drops chronos.db back to the
committed, already-seeded copy rather than re-running this.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CHRONOS_SQLITE", str(ROOT / "demo" / "novapay" / ".chronos" / "chronos.db"))

from chronos import ledger


def main():
    con = ledger.connect()

    # Session 1 -- query-only. No lock, no write: chronos_who_touched calls
    # don't mutate the ledger, so the only footprint of a query-only session
    # is provenance events logged with action="queried".
    ledger.log_event(con, "src/payments/service.py::process_payment::Function",
                     agent_id="claude-code", session_id="session-1",
                     action="queried", reason="exploring payment flow before adding webhook retries")
    ledger.log_event(con, "src/payments/client.py::charge_card::Function",
                     agent_id="claude-code", session_id="session-1",
                     action="queried", reason="checking current httpx pattern")

    # Session 2 -- write session, correct pattern, clean lock lifecycle.
    node2 = "src/payments/webhooks.py::verify_signature::Function"
    ledger.acquire(con, node2, agent_id="claude-code", session_id="session-2",
                   intent="add HMAC signature verification for provider webhooks")
    ledger.log_event(con, node2, agent_id="claude-code", session_id="session-2",
                     action="modified", reason="added verify_signature using httpx-fetched provider keys")
    ledger.release(con, node2, agent_id="claude-code", session_id="session-2")

    # Session 3 -- write session, blocked by CI. Lock acquired and released
    # (the agent finished and moved on before CI ran), but the write itself
    # got a real no-requests block -- mirrors demo/run_comparison.py's
    # WITHOUT-Chronos scenario, logged here as ledger history instead of a
    # one-off trace.json.
    node3 = "src/payments/new_endpoint.py::notify_provider_sync::Function"
    ledger.acquire(con, node3, agent_id="cursor", session_id="session-3",
                   intent="add payment notify endpoint")
    ledger.log_event(con, node3, agent_id="cursor", session_id="session-3",
                     action="modified", reason="reused requests.post pattern from legacy_notify.py")
    ledger.log_event(con, node3, agent_id="cursor", session_id="session-3",
                     action="blocked", reason="chronos-enforce: no-requests -- pattern matches "
                     "deprecated node confirmed by temporal graph")
    ledger.release(con, node3, agent_id="cursor", session_id="session-3")

    con.commit()
    rows = con.execute("SELECT node_id, agent_id, session_id, action, reason "
                       "FROM provenance_events ORDER BY id").fetchall()
    for r in rows:
        print(dict(r))


if __name__ == "__main__":
    main()
