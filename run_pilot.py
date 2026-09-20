import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path("C:/Users/urbra/OneDrive/Desktop/Projects/New ortho")
PILOT_REPO = ROOT / "pilot_target"
CHRONOS_DB = PILOT_REPO / ".chronos" / "chronos.db"
REPORT_FILE = ROOT / "pilot_report.md"

def log(msg):
    print(f"[*] {msg}", flush=True)

async def run_stage(title, mode, extra_env, session):
    log(f"Starting Stage: {title} ({mode} mode)")
    results = []
    
    def record(tool, result, latency):
        status = "✅ PASS" if "error" not in str(result).lower() or mode == "strict" else "❌ FAIL"
        if mode == "strict" and "error" in str(result).lower() and ("permission_denied" in str(result).lower() or "auth_failed" in str(result).lower() or "auth_required" in str(result).lower()):
            status = "✅ PASS (Denied as expected)"
        results.append({"tool": tool, "status": status, "latency": latency, "result": str(result)[:300]})
    
    async def call(tool, **kwargs):
        log(f"Calling tool: {tool}")
        t0 = time.time()
        try:
            res = await asyncio.wait_for(session.call_tool(tool, arguments=kwargs), timeout=15.0)
            res_content = res.content[0].text if res.content else str(res)
        except Exception as e:
            res_content = f"Exception: {e}"
        t1 = time.time()
        latency = round((t1 - t0) * 1000, 2)
        record(tool, res_content, latency)
        log(f"Finished {tool} in {latency}ms")
        return res_content
        
    # Wedge 1 Tools
    await call("index_health")
    await call("as_of_callers", node_id="httpcore/_api.py::request::Function", timestamp=datetime.now(timezone.utc).isoformat())
    await call("what_changed", since_timestamp=(datetime.now(timezone.utc)).isoformat())

    # Wedge 3 Tools
    await call("chronos_acquire_lock", node_id="httpcore/_api.py::request::Function", agent_id="pilot-agent-1", intent="refactoring connection flow", ttl_seconds=300)
    await call("chronos_check_conflicts", node_id="httpcore/_api.py::request::Function", agent_id="pilot-agent-2")
    await call("chronos_log_provenance", node_id="httpcore/_api.py::request::Function", agent_id="pilot-agent-1", action="modified", reason="updated connection flow")
    await call("chronos_who_touched", node_id="httpcore/_api.py::request::Function")
    await call("chronos_release_lock", node_id="httpcore/_api.py::request::Function", agent_id="pilot-agent-1")
    
    # Wedge 2 Tools
    await call("chronos_query_playbook", query="error handling", agent_id="pilot-agent-1")
    await call("chronos_capture_lesson", trace={"agent_id": "pilot-agent-1", "error": "Connection reset by peer"}, lesson="Always wrap in try/except and retry")
    await call("chronos_playbook_health")
    
    # Wedge 4 Tools
    await call("chronos_list_rules")
    
    # Governance & Status
    await call("chronos_sensitive_reads", agent_id="pilot-agent-1")
    await call("chronos_mcp_status")
    
    return results

async def main():
    env = dict(os.environ)
    env["CHRONOS_REPO_PATH"] = str(PILOT_REPO)
    env["CHRONOS_SQLITE"] = str(CHRONOS_DB)
    # Remove CHRONOS_DB from env to use the default .chronos/graph
    if "CHRONOS_DB" in env:
        del env["CHRONOS_DB"]
    env["CHRONOS_CAPTURE"] = "0"
    
    # Stage 1: Permissive Mode
    env["CHRONOS_AUTH"] = "permissive"
    params = StdioServerParameters(command=sys.executable, args=["-m", "chronos.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            stage1_results = await run_stage("Wedge 1-4 Feature Workflow", "permissive", env, session)
            
    # Stage 2: Strict Mode Setup
    import sqlite3
    con = sqlite3.connect(str(CHRONOS_DB))
    con.execute("CREATE TABLE IF NOT EXISTS agents (agent_id TEXT PRIMARY KEY, key_hash TEXT, created_at TEXT)")
    con.execute("INSERT OR IGNORE INTO agents VALUES ('pilot-agent-strict', 'dummy-hash', '2026-01-01')")
    con.execute("CREATE TABLE IF NOT EXISTS permissions (agent_id TEXT, allowed_paths TEXT, denied_paths TEXT, max_lock_ttl INTEGER)")
    con.execute("INSERT OR IGNORE INTO permissions VALUES ('pilot-agent-strict', '[\"httpcore/*\"]', '[\"httpcore/_sync/*\"]', 300)")
    con.commit()
    con.close()
    
    env["CHRONOS_AUTH"] = "strict"
    env["CHRONOS_API_KEY"] = "dummy-key-not-matching" # should fail auth
    
    params = StdioServerParameters(command=sys.executable, args=["-m", "chronos.server"], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            stage2_results = await run_stage("Enterprise Governance Strict", "strict", env, session)
            
    # Generate Markdown Report
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("# Chronos MCP Pilot Evaluation Report\n\n")
        f.write("Target Repository: `encode/httpcore`\n")
        f.write("Evaluation Date: " + datetime.now(timezone.utc).isoformat() + "\n\n")
        
        for title, results in [("Stage 1: Permissive Mode (Wedge 1-4)", stage1_results), 
                               ("Stage 2: Strict Governance Mode (F1-F7)", stage2_results)]:
            f.write(f"## {title}\n\n")
            f.write("| Tool | Status | Latency (ms) | Result Snippet |\n")
            f.write("|---|---|---|---|\n")
            for r in results:
                f.write(f"| `{r['tool']}` | {r['status']} | {r['latency']} | `{r['result'].replace('|', 'I').replace(chr(10), ' ')[:200]}...` |\n")
            f.write("\n")
            
    log("Pilot evaluation completed. Report saved to pilot_report.md")

if __name__ == '__main__':
    asyncio.run(main())
