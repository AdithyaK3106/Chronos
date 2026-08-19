"""Runs the WITHOUT-Chronos and WITH-Chronos traces for the task "add a new
endpoint that calls the payment processing service," against the real
demo/novapay repo, and writes demo/trace.json for the HTML player to read.

WITHOUT-Chronos side: a scripted, reproducible sequence of real file reads
and greps (the exploration path a coding agent takes without a call-graph
tool) -- not live freewheeling, so the demo doesn't vary between runs. Token
counts are the real byte size of each file read, converted at ~4 chars/token
(the standard rough estimate; no tokenizer dependency needed for a demo).

WITH-Chronos side: two real Chronos MCP tool calls (as_of_callees,
as_of_diff) against the real indexed graph -- not mocked.

ponytail: one-shot script for one demo artifact, not a general trace
recorder. Re-run any time to regenerate trace.json.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOVAPAY = ROOT / "demo" / "novapay"
sys.path.insert(0, str(ROOT))


def toks(text: str) -> int:
    return max(1, len(text) // 4)


# Blended input+output rate for a mid-tier coding-agent model, $/1M tokens.
# Stated explicitly (not hidden in a multiply) so the number is auditable,
# not a black box -- swap this constant for your own model's pricing.
USD_PER_1M_TOKENS = 6.00


def cost_usd(total_tokens: int) -> float:
    return round(total_tokens * USD_PER_1M_TOKENS / 1_000_000, 4)


def files_touched(events: list[dict]) -> int:
    """Distinct files whose raw source had to be read into context.

    MCP calls return structured query results, not file contents, so they
    don't count -- that's the real difference this metric is meant to show."""
    return len({e["target"] for e in events if e["action"] == "read_file"})


# --- WITHOUT Chronos: scripted exploration, mirrors what an agent without a
# call-graph tool actually does for "add an endpoint that calls payments" ---
WITHOUT_STEPS = [
    ("read_file", "src/main.py"),
    ("read_file", "src/orders/routes.py"),
    ("read_file", "src/orders/service.py"),
    ("grep_search", "payment"),
    ("read_file", "src/payments/service.py"),
    ("read_file", "src/payments/client.py"),
    ("read_file", "src/payments/ledger.py"),
    ("grep_search", "requests"),
    ("read_file", "src/payments/webhooks.py"),
    ("read_file", "src/notifications/client.py"),
    ("grep_search", "httpx"),
    ("read_file", "docs/migration-guide.md"),
    ("read_file", "src/users/routes.py"),
    ("read_file", "src/orders/repository.py"),
    ("read_file", "src/db.py"),
    ("read_file", "src/orders/schemas.py"),
    ("read_file", "src/payments/service.py"),  # re-read, partial recall
    ("read_file", "src/payments/client.py"),   # re-read to double check pattern
]

GREP_HIT_BYTES = 220  # a few matching lines' worth of context returned per grep


def run_without():
    events = []
    total_tokens = 0
    t0 = time.time()
    for action, target in WITHOUT_STEPS:
        if action == "read_file":
            content = (NOVAPAY / target).read_text(encoding="utf-8")
            cost = toks(content)
        else:
            cost = toks("x" * GREP_HIT_BYTES)
        total_tokens += cost
        events.append({
            "t": round(time.time() - t0, 2),
            "action": action,
            "target": target,
            "tokens": cost,
            "running_tokens": total_tokens,
            "tool_call_n": len(events) + 1,
        })
    # Final code: reuses the still-visible `requests` pattern AND the old
    # function name it copied from legacy_notify.py -- correct per most of
    # the codebase, wrong per the actual current standard. The function name
    # matters: it's what the no-requests rule's $NAME capture matches against
    # the graph, and notify_provider_sync is confirmed retired there (both
    # of its facts are closed) -- see docs/migration-guide.md.
    final_code = (
        "import requests\n\n"
        "def notify_provider_sync(order_id, amount_cents):\n"
        "    resp = requests.post(PAYMENT_URL, json={\"order_id\": order_id, \"amount\": amount_cents})\n"
        "    return resp.json()\n"
    )
    events.append({
        "t": round(time.time() - t0, 2), "action": "write_code", "target": "src/payments/new_endpoint.py",
        "tokens": toks(final_code), "running_tokens": total_tokens + toks(final_code),
        "tool_call_n": len(events) + 1, "code": final_code,
    })
    n_tokens = total_tokens + toks(final_code)
    n_calls = len(events) + 1
    n_files = files_touched(events)
    return {
        "events": events,
        "total_tokens": n_tokens,
        "tool_calls": n_calls,
        "files_touched": n_files,
        "calls_per_file": round(n_calls / n_files, 1) if n_files else None,
        "cost_usd": cost_usd(n_tokens),
        "elapsed_s": round(time.time() - t0, 2),
        "final_code": final_code,
        "uses_deprecated_pattern": True,
        "shipped_correct_pattern": False,
    }


# --- WITH Chronos: two real MCP tool calls against the real indexed graph ---
async def run_with():
    from chronos.store import open_driver
    from chronos import query

    import os
    os.environ["CHRONOS_DB"] = str(NOVAPAY / ".chronos" / "graph.kz")
    group = "c-users-urbra-onedrive-desktop-projects-new-ortho-demo-novapay"

    events = []
    total_tokens = 0
    t0 = time.time()

    drv = open_driver()
    try:
        r1 = await query.callees(drv, group, "process_payment")
        j1 = json.dumps(r1, default=str)
        c1 = toks(j1)
        total_tokens += c1
        events.append({
            "t": round(time.time() - t0, 2), "action": "mcp_call", "target": "as_of_callees(process_payment)",
            "tokens": c1, "running_tokens": total_tokens, "tool_call_n": 1, "result_preview": j1[:300],
        })

        r2 = await query.callers_diff(
            drv, group, "notify_provider_sync",
            since=__import__("datetime").datetime(2026, 6, 3, tzinfo=__import__("datetime").timezone.utc),
            until=__import__("datetime").datetime(2026, 6, 20, tzinfo=__import__("datetime").timezone.utc),
        )
        j2 = json.dumps(r2, default=str)
        c2 = toks(j2)
        total_tokens += c2
        events.append({
            "t": round(time.time() - t0, 2), "action": "mcp_call", "target": "as_of_diff(notify_provider_sync, since=2026-06-03, until=2026-06-20)",
            "tokens": c2, "running_tokens": total_tokens, "tool_call_n": 2, "result_preview": j2[:300],
        })
    finally:
        await drv.close()

    final_code = (
        "import httpx\n\n"
        "async def notify_payment_service(order_id, amount_cents):\n"
        "    async with httpx.AsyncClient() as client:\n"
        "        resp = await client.post(PAYMENT_URL, json={\"order_id\": order_id, \"amount\": amount_cents})\n"
        "        return resp.json()\n"
    )
    events.append({
        "t": round(time.time() - t0, 2), "action": "write_code", "target": "src/payments/new_endpoint.py",
        "tokens": toks(final_code), "running_tokens": total_tokens + toks(final_code),
        "tool_call_n": 3, "code": final_code,
    })

    n_tokens = total_tokens + toks(final_code)
    n_files = files_touched(events)  # 0: MCP calls return graph facts, never raw file contents
    return {
        "events": events,
        "total_tokens": n_tokens,
        "tool_calls": 3,
        "files_touched": n_files,
        "calls_per_file": None,
        "cost_usd": cost_usd(n_tokens),
        "elapsed_s": round(time.time() - t0, 2),
        "final_code": final_code,
        "uses_deprecated_pattern": False,
        "shipped_correct_pattern": True,
    }


# --- Real CI enforcement against the WITHOUT-Chronos agent's output: a real
# `enforce_files` call (chronos/cli.py) against the real `no-requests` rule,
# not a hardcoded HTML block. ---
async def run_enforce(final_code: str) -> dict:
    from chronos.cli import enforce_files
    from chronos.store import open_driver

    import os
    os.environ.setdefault("CHRONOS_DB", str(NOVAPAY / ".chronos" / "graph.kz"))
    group = "c-users-urbra-onedrive-desktop-projects-new-ortho-demo-novapay"

    target = NOVAPAY / "src" / "payments" / "new_endpoint.py"
    target.write_text(final_code, encoding="utf-8")
    try:
        drv = open_driver()
        try:
            report = await enforce_files(
                ["src/payments/new_endpoint.py"], str(NOVAPAY), group=group, driver=drv,
            )
        finally:
            await drv.close()
    finally:
        target.unlink(missing_ok=True)
    return report


def main():
    without = run_without()
    withc = asyncio.run(run_with())
    enforce_report = asyncio.run(run_enforce(without["final_code"]))

    out = {
        "task": "Add a new endpoint that calls the payment processing service.",
        "without_chronos": without,
        "with_chronos": withc,
        "enforce": enforce_report,
        "summary": {
            "token_savings_pct": round(100 * (1 - withc["total_tokens"] / without["total_tokens"])),
            "tool_call_ratio": f"{without['tool_calls']}:{withc['tool_calls']}",
            "cost_savings_pct": round(100 * (1 - withc["cost_usd"] / without["cost_usd"])),
            "usd_per_1m_tokens": USD_PER_1M_TOKENS,
            "files_touched_ratio": f"{without['files_touched']}:{withc['files_touched']}",
        },
    }
    out_path = ROOT / "demo" / "trace.json"
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"WITHOUT: {without['total_tokens']} tokens, {without['tool_calls']} calls, "
          f"{without['files_touched']} files, ${without['cost_usd']}")
    print(f"WITH:    {withc['total_tokens']} tokens, {withc['tool_calls']} calls, "
          f"{withc['files_touched']} files, ${withc['cost_usd']}")
    print(f"savings: {out['summary']['token_savings_pct']}% tokens, "
          f"{out['summary']['cost_savings_pct']}% cost")
    print(f"ENFORCE: {enforce_report['blocks']} block, {enforce_report['warns']} warn, "
          f"{enforce_report['oks']} ok -- {enforce_report['rows']}")


if __name__ == "__main__":
    main()
