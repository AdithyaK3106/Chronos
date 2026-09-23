import os
import subprocess
import time
import json
import re

# Mock SWE-bench subset for the "Smoke Test"
TASKS = [
    {
        "id": "django-11099", 
        "prompt": "Fix the bug where the authentication validator rejects valid regex patterns. Navigate the codebase to find the validator and correct it."
    },
    {
        "id": "requests-3390",
        "prompt": "Fix the issue where a mock response payload throws a chunked encoding error. Find the dependency and patch the chunk reader."
    }
]

def extract_metrics_from_output(output):
    """
    Parses OpenCode terminal output to extract token usage and tool calls.
    (Adjust the regex based on exactly how OpenCode prints its token usage)
    """
    tokens = 0
    tool_calls = 0
    
    # Example regex (you may need to tweak this to match OpenCode's exact output)
    token_match = re.search(r'(?i)tokens? used:?\s*([\d,]+)', output)
    if token_match:
        tokens = int(token_match.group(1).replace(',', ''))
        
    tool_match = re.search(r'(?i)tool calls:?\s*([\d]+)', output)
    if tool_match:
        tool_calls = int(tool_match.group(1))
        
    return {"tokens": tokens, "tool_calls": tool_calls}

def run_agent(task_prompt, use_chronos=False):
    print(f"  -> Launching OpenCode (Chronos Enabled: {use_chronos})...")
    start_time = time.time()
    
    # We orchestrate the CLI directly. 
    # If Chronos is enabled, we append the instructions to use it.
    prompt = task_prompt
    if use_chronos:
        prompt += "\n\nIMPORTANT: You are connected to the Chronos MCP server. You MUST use 'as_of_callers' and 'as_of_callees' to navigate the codebase instead of generic searches."
        
    command = ["opencode", "--task", prompt]
    
    try:
        # Run OpenCode as a subprocess and capture its terminal output
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600 # 10 minute timeout per task
        )
        output = result.stdout + "\n" + result.stderr
    except subprocess.TimeoutExpired:
        output = "TIMEOUT"
        
    latency = round(time.time() - start_time, 2)
    metrics = extract_metrics_from_output(output)
    metrics["latency_seconds"] = latency
    
    return metrics, output

def main():
    print("==================================================")
    print(" Chronos Efficacy Benchmark (Powered by OpenCode) ")
    print("==================================================")
    
    results = []
    
    for task in TASKS:
        print(f"\n[Running Task]: {task['id']}")
        
        print("  [Phase 1]: Baseline (Standard OpenCode)")
        base_metrics, base_out = run_agent(task['prompt'], use_chronos=False)
        print(f"  -> Tokens: {base_metrics.get('tokens', 'N/A')} | Time: {base_metrics['latency_seconds']}s")
        
        # Note: In a real run, you would `git checkout .` here to reset the codebase
        
        print("  [Phase 2]: Chronos-Enhanced")
        chronos_metrics, chronos_out = run_agent(task['prompt'], use_chronos=True)
        print(f"  -> Tokens: {chronos_metrics.get('tokens', 'N/A')} | Time: {chronos_metrics['latency_seconds']}s")
        
        results.append({
            "task_id": task["id"],
            "baseline": base_metrics,
            "chronos": chronos_metrics
        })
        
    with open("reports/efficacy_results.json", "w") as f:
        json.dump(results, f, indent=2)
        
    print("\nBenchmark complete! Results saved to reports/efficacy_results.json")

if __name__ == "__main__":
    main()
