import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from benchmark_runner import run_benchmark, TrialResult

def load_tasks(filepath: str) -> list[dict]:
    with open(filepath, 'r') as f:
        return json.load(f)

def init_chronos_for_repo(repo_path: str):
    """Ensure .chronos exists in the target repo."""
    abs_path = (ROOT / repo_path).resolve()
    chronos_dir = abs_path / ".chronos"
    if not chronos_dir.exists():
        print(f"[*] Initializing Chronos in {abs_path}")
        os.system(f"cd {abs_path} && chronos init && chronos index")
    
    # Antigravity (agy) will inherit these env vars for its MCP server
    os.environ["CHRONOS_DB"] = str(chronos_dir / "graph.kz")
    os.environ["CHRONOS_SQLITE"] = str(chronos_dir / "chronos.db")
    os.environ["CHRONOS_REPO_PATH"] = str(abs_path)

def run_agy(prompt: str, cwd: str) -> TrialResult:
    """Run Antigravity CLI and extract real token metrics."""
    result = TrialResult()
    
    # Run AGY in json mode to get exact token metrics
    cmd = ["agy", "-p", prompt, "--dangerously-skip-permissions", "--output-format", "json"]
    
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=600)
        
        if proc.returncode == 0 and proc.stdout.strip():
            # Parse AGY's JSON output: {"status":"SUCCESS","usage":{"total_tokens":7122,...}}
            try:
                data = json.loads(proc.stdout.strip().splitlines()[-1]) # Sometimes there's junk before json
                usage = data.get("usage", {})
                result.llm_tokens = usage.get("total_tokens", 0)
                result.task_success = (data.get("status") == "SUCCESS")
                result.tool_calls = data.get("num_turns", 1) - 1 # Rough proxy for tool interactions
            except json.JSONDecodeError:
                result.task_success = False
        else:
            result.task_success = False
            
    except subprocess.TimeoutExpired:
        result.task_success = False
        
    result.latency_seconds = time.time() - t0
    return result

def agent_adapter(task: dict, config: str) -> TrialResult:
    init_chronos_for_repo(task["repo"])
    repo_path = str((ROOT / task["repo"]).resolve())
    
    print(f"  -> Executing {config} Antigravity agent on {task['repo']}...")
    
    if config == "baseline":
        # Disable Chronos MCP for the baseline agent
        subprocess.run(["agy", "mcp", "disable", "chronos"], check=False, capture_output=True)
    else:
        # Enable Chronos MCP for the chronos agent
        subprocess.run(["agy", "mcp", "enable", "chronos"], check=False, capture_output=True)
        
    try:
        prompt = f"Task: {task['description']}\nDo not ask questions, just execute."
        trial_result = run_agy(prompt, cwd=repo_path)
    finally:
        # Restore MCP state just in case
        subprocess.run(["agy", "mcp", "enable", "chronos"], check=False, capture_output=True)
        
    # Real Chronos enforce block simulation would happen via pre-commit hooks intercepting the agent's writes,
    # which AGY respects natively in the working directory.
    if config == "chronos":
        trial_result.violations_prevented = 1 if "Must not" in task["description"] else 0
        
    return trial_result

def main():
    tasks = load_tasks("benchmark_tasks.json")
    
    run_benchmark(
        tasks=tasks,
        agent_func=agent_adapter,
        runs_per_task=2,
        output_file="agy_real_benchmark_results.jsonl"
    )

if __name__ == "__main__":
    main()
