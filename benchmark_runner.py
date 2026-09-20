import json
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Any, List

# Core metrics expected per run
class TrialResult:
    def __init__(self):
        self.task_success: bool = False
        self.llm_tokens: int = 0
        self.tool_calls: int = 0
        self.latency_seconds: float = 0.0
        self.violations_prevented: int = 0
        self.temporal_correctness: bool = False
        self.coordination_conflicts: int = 0

def run_benchmark(
    tasks: List[Dict[str, Any]],
    agent_func: Callable[[Dict[str, Any], str], TrialResult],
    runs_per_task: int = 3,
    output_file: str = "benchmark_results.jsonl"
):
    """
    Minimal credible benchmark runner for Baseline vs Chronos.
    DOES NOT implement a 16-way ablation matrix.
    DOES implement 3-5 runs per task for `baseline` and `chronos` configs.
    """
    out_path = Path(output_file)
    if out_path.exists():
        out_path.unlink()

    results = []

    print(f"Starting benchmark across {len(tasks)} tasks...")
    
    for task in tasks:
        for config in ["baseline", "chronos"]:
            for run_id in range(1, runs_per_task + 1):
                print(f"Running Task {task['id']} | Config: {config} | Trial: {run_id}/{runs_per_task}")
                
                t0 = time.time()
                # agent_func must return a TrialResult
                trial_metrics = agent_func(task, config)
                elapsed = time.time() - t0
                
                record = {
                    "task_id": task["id"],
                    "repository": task.get("repo", "unknown"),
                    "config": config,
                    "model": task.get("model", "unknown"),
                    "run_number": run_id,
                    "metrics": {
                        "task_success": trial_metrics.task_success,
                        "llm_tokens": trial_metrics.llm_tokens,
                        "tool_calls": trial_metrics.tool_calls,
                        "latency_seconds": round(elapsed, 2),
                        "violations_prevented": trial_metrics.violations_prevented,
                        "temporal_correctness": trial_metrics.temporal_correctness,
                        "coordination_conflicts": trial_metrics.coordination_conflicts
                    }
                }
                results.append(record)
                
                with open(out_path, "a") as f:
                    f.write(json.dumps(record) + "\n")

    # Aggregate Results
    print("\n--- Aggregated Results ---")
    _aggregate_results(results)

def _aggregate_results(results: List[Dict[str, Any]]):
    aggregated = {"baseline": {}, "chronos": {}}
    
    for record in results:
        cfg = record["config"]
        task_id = record["task_id"]
        
        if task_id not in aggregated[cfg]:
            aggregated[cfg][task_id] = {
                "successes": 0, "runs": 0,
                "tokens": [], "latency": [], "tool_calls": []
            }
            
        agg = aggregated[cfg][task_id]
        m = record["metrics"]
        
        agg["runs"] += 1
        if m["task_success"]:
            agg["successes"] += 1
        agg["tokens"].append(m["llm_tokens"])
        agg["latency"].append(m["latency_seconds"])
        agg["tool_calls"].append(m["tool_calls"])

    # Output mean/median and success rate
    for cfg in ["baseline", "chronos"]:
        print(f"\nConfiguration: {cfg.upper()}")
        for task_id, agg in aggregated[cfg].items():
            success_rate = (agg["successes"] / agg["runs"]) * 100
            mean_tokens = statistics.mean(agg["tokens"])
            med_tokens = statistics.median(agg["tokens"])
            mean_latency = statistics.mean(agg["latency"])
            
            print(f"  Task: {task_id}")
            print(f"    Success Rate: {success_rate:.1f}% ({agg['successes']}/{agg['runs']})")
            print(f"    Tokens (Mean / Median): {mean_tokens:.0f} / {med_tokens:.0f}")
            print(f"    Latency (Mean): {mean_latency:.1f}s")
