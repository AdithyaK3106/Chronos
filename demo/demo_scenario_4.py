"""make demo-scenario-4 -- "Human in the Loop": an agent tries to touch
NovaPay's payment-provider credentials, gets stopped by a sensitive-module
gate (F5) instead of writing straight through, a human approves it from the
CLI, and the agent's retry succeeds.

Real calls throughout: chronos_acquire_lock via wedge3_mcp's ledger.acquire,
then `chronos gates approve` (the same CLI command demo/seed_governance.py's
printed hint tells an agent to run) -- not a canned transcript.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NOVAPAY = ROOT / "demo" / "novapay"
os.environ.setdefault("CHRONOS_SQLITE", str(NOVAPAY / ".chronos" / "chronos.db"))
os.environ.setdefault("CHRONOS_REPO_PATH", str(NOVAPAY))

from chronos import gates, ledger  # noqa: E402


def main():
    con = ledger.connect()
    node = "src/payments/secrets/provider_keys.py::rotate_key::Function"
    agent = "cursor"

    print(f"$ chronos_acquire_lock(node_id={node!r}, agent_id={agent!r})\n")
    result = ledger.acquire(con, node, agent_id=agent, session_id="scenario-4",
                            intent="rotate the payment provider's API key")
    if result.get("reason") != "gate_pending":
        # Already gated from a prior run (demo-reset not called) -- fall through
        # to showing whatever gate exists on this node instead of erroring.
        pending = [g for g in gates.list_pending(con) if g["node_id"] == node]
        if not pending:
            print(f"unexpected result (run `make demo-reset` first): {result}")
            sys.exit(1)
        gate_id = pending[0]["gate_id"]
    else:
        gate_id = result["gate_id"]
        print(f"BLOCKED -- {result['message']}\n")
        print(f"gate_id: {gate_id}")
        print(f"module:  {result['module']}\n")

    print(f"$ chronos_check_gate_status(gate_id={gate_id!r})")
    gate = gates.get_gate(gate_id, con)
    print(f"  status: {gate['status']}\n")

    print(f"--- a human runs this from a terminal with repo access ---")
    print(f"$ chronos gates approve {gate_id}\n")
    approval = gates.approve(gate_id, "demo-operator", con)
    print(f"  {approval}\n")

    print(f"$ chronos_acquire_lock(node_id={node!r}, agent_id={agent!r})  # retry")
    retry = ledger.acquire(con, node, agent_id=agent, session_id="scenario-4",
                           intent="rotate the payment provider's API key")
    print(f"  {retry}")
    if retry.get("acquired") and retry.get("renewed"):
        print("\nacquired + renewed=True -- the gate no longer blocks this agent.")
        ledger.release(con, node, agent_id=agent, session_id="scenario-4")
    else:
        print(f"\nunexpected: expected acquired+renewed, got {retry}")
        sys.exit(1)


if __name__ == "__main__":
    main()
