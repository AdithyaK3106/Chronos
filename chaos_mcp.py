import asyncio
import os
import sys
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path("C:/Users/urbra/OneDrive/Desktop/Projects/New ortho")
PILOT_REPO = ROOT / "pilot_target"
CHRONOS_DB = PILOT_REPO / ".chronos" / "chronos.db"
REPORT_FILE = ROOT / "chaos_report.md"

async def call(session, tool, **kwargs):
    try:
        res = await asyncio.wait_for(session.call_tool(tool, arguments=kwargs), timeout=10.0)
        content = res.content[0].text if res.content else str(res)
        return {"tool": tool, "status": "OK", "response": content[:150]}
    except asyncio.TimeoutError:
        return {"tool": tool, "status": "TIMEOUT", "response": "Request timed out"}
    except Exception as e:
        return {"tool": tool, "status": "ERROR", "response": str(e)[:150]}

async def run_chaos():
    env = dict(os.environ)
    env["CHRONOS_REPO_PATH"] = str(PILOT_REPO)
    env["CHRONOS_SQLITE"] = str(CHRONOS_DB)
    if "CHRONOS_DB" in env: del env["CHRONOS_DB"]
    env["CHRONOS_AUTH"] = "permissive"
    env["CHRONOS_CAPTURE"] = "0"

    print("[*] Starting MCP Chaos Test (Concurrency: 50)...")
    
    params = StdioServerParameters(command=sys.executable, args=["-m", "chronos.server"], env=env)
    
    results = []
    
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            
            # --- WEDGE 1: Time Travel & Graph (Invalid Timestamps, Concurrency) ---
            print("[*] Stressing Wedge 1 (Graph & Time Travel)")
            w1_tasks = [
                call(session, "what_changed", since_timestamp="9999-99-99T99:99:99Z"), # Malformed
                call(session, "what_changed", since_timestamp="1970-01-01T00:00:00Z"), # Epoch
                call(session, "as_of_callers", node_id="httpcore/_api.py::request::Function", timestamp="invalid_date_string"),
            ]
            results.extend(await asyncio.gather(*w1_tasks))

            # --- WEDGE 3: Intent Ledger & Locking (Lock Spams, Max TTLs, Path Traversal) ---
            print("[*] Stressing Wedge 3 (Lock Storms & Escapes)")
            w3_tasks = []
            for i in range(50):
                # Request absurd TTLs and path traversals
                w3_tasks.append(call(session, "chronos_acquire_lock", node_id=f"../../../C:/Windows/System32/fake_{i}.dll", agent_id="chaos-agent", ttl_seconds=999999999, intent="chaos"))
            
            # Fire all 50 lock requests concurrently
            lock_results = await asyncio.gather(*w3_tasks)
            results.extend(lock_results[:5]) # just record a few for the report
            results.append({"tool": "chronos_acquire_lock (x50)", "status": "STORM_COMPLETE", "response": f"Fired 50 concurrent locks. Errors: {sum(1 for r in lock_results if r['status'] == 'ERROR')}"})

            # --- WEDGE 2: Playbook (Massive Payloads) ---
            print("[*] Stressing Wedge 2 (Buffer Bloating)")
            giant_lesson = "A" * (1024 * 500) # 500KB string payload
            results.append(await call(session, "chronos_capture_lesson", trace={"agent_id": "chaos-agent"}, lesson=giant_lesson))

            # --- WEDGE 4: CI Enforcement (Malformed AST / Empty Diffs) ---
            print("[*] Stressing Wedge 4 (Malformed Enforcement & Rules)")
            results.append(await call(session, "chronos_enforce", diff_text="@@ invalid diff format @@\n+++ a/file.py", lang="python"))
            results.append(await call(session, "chronos_generate_rule", bad_code="def bad(): pass", good_code="def good(): pass", error_message="A" * 10000))
            results.append(await call(session, "chronos_promote_rule", rule_id="non-existent-rule-uuid"))

            # --- GOVERNANCE (F5, F6, F7) & SQLite Contention ---
            print("[*] Stressing Governance (F5/F6/F7 Contention)")
            gov_tasks = [
                call(session, "chronos_check_gate_status", gate_id="not-a-uuid"),
                call(session, "chronos_trigger_baseline_recompute"), # Heavy SQLite operation
                call(session, "chronos_sensitive_reads", agent_id="chaos-agent", since_hours=-999), # Negative bounds
                call(session, "chronos_anomaly_report", agent_id="chaos-agent"),
                # Status check during load
                call(session, "chronos_mcp_status")
            ]
            results.extend(await asyncio.gather(*gov_tasks))

    print("[*] Generating Markdown Report...")
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("# Chronos MCP Chaos Test Report\n\n")
        f.write("| Component Tested | Tool | Result | MCP Status | Response Snippet |\n")
        f.write("|---|---|---|---|---|\n")
        for r in results:
            clean_res = r['response'].replace('|', 'I').replace(chr(10), ' ')[:200]
            f.write(f"| Chaos Target | `{r['tool']}` | {r['status']} | **{r['status']}** | `{clean_res}` |\n")

    print("[*] Done.")

if __name__ == "__main__":
    asyncio.run(run_chaos())
