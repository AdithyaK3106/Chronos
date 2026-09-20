import os
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent
NOVAPAY = ROOT / "demo" / "novapay"
sys.path.insert(0, str(ROOT))
os.environ["CHRONOS_SQLITE"] = str(NOVAPAY / ".chronos" / "chronos.db")
os.environ["CHRONOS_REPO_PATH"] = str(NOVAPAY)
from chronos import ledger
con = ledger.connect()
node = "src/payments/secrets/provider_keys.py::rotate_key::Function"
agent = "cursor"
result = ledger.acquire(con, node, agent_id=agent, session_id="live-demo", intent="rotate the payment provider's API key")
print("Gate created:", result)
