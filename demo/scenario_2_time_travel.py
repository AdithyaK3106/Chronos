"""make demo-scenario-2 -- "Time Travel Query": ask the bi-temporal graph what
process_payment called before the requests->httpx refactor (commit b25311b,
2026-06-15, which also retired legacy_notify.py's notify_provider_sync
entirely), then ask again with no `at`, and see the two real, different
answers.

Real MCP query calls (chronos/query.py::callees) against the committed
demo/novapay/.chronos/graph.kz -- not mocked.
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOVAPAY = ROOT / "demo" / "novapay"
sys.path.insert(0, str(ROOT))

GROUP = "novapay-demo"
BEFORE_REFACTOR = datetime(2026, 6, 12, tzinfo=timezone.utc)  # refactor landed 2026-06-15


async def main():
    os.environ.setdefault("CHRONOS_DB", str(NOVAPAY / ".chronos" / "graph.kz"))
    from chronos.store import open_driver
    from chronos import query

    drv = open_driver()
    try:
        print(f"chronos query --as-of {BEFORE_REFACTOR.date()} --symbol process_payment\n")
        before = await query.callees(drv, GROUP, "process_payment", at=BEFORE_REFACTOR)
        print(json.dumps(before, indent=2, default=str))

        print("\nchronos query --symbol process_payment   (current)\n")
        current = await query.callees(drv, GROUP, "process_payment")
        print(json.dumps(current, indent=2, default=str))
    finally:
        await drv.close()


if __name__ == "__main__":
    asyncio.run(main())
