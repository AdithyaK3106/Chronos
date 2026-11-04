"""Does Chronos memory help an agent finish tasks? (suite memory_v1, Chronos arm only)

Five shopkit tasks, each with a house rule an agent can discover by reading the code. Every task
runs in three variants with the same prompt:
  mem     the relevant memory is stored; Chronos's recall decides whether to inject it (as built)
  forced  the memory is always injected (does the content help once it arrives?)
  nomem   empty memory store
After each nomem run the harness's post-task distillation is saved, to check whether automatic
capture records the house rule.

    python -m benchmarks.memory_ab --setup
    python -m benchmarks.memory_ab --runs 2 --json artifacts/memory_ab_2026-11-04.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
from pathlib import Path

SUITE = Path(__file__).parent / "suites" / "memory_v1"
REPO_SRC = SUITE / "repo"
RESULTS = Path(__file__).parent / "runs" / "memory_v1"
# Words a distilled memory must contain to count as having captured each house rule.
RULE_WORDS = {
    "gen": ["gen_client", "spec/api.yaml", "generated"], "money": ["de.json", "percent_of", "locale"],
    "clock": ["clock.now", "set_now"], "flags": ["flags.yaml", "is_enabled"], "errors": ["error_codes", "CODES"],
}


def repo_path() -> Path:
    return Path(os.environ.get("MEMORY_SHOPKIT_PATH") or Path.home() / ".cache" / "chronos-bench" / "memory_shopkit")


def setup() -> str:
    dest = repo_path()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(REPO_SRC, dest, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    env = {**os.environ, "GIT_AUTHOR_DATE": "2026-11-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-11-01T00:00:00+00:00",
           "GIT_AUTHOR_NAME": "shopkit", "GIT_AUTHOR_EMAIL": "dev@shopkit", "GIT_COMMITTER_NAME": "shopkit", "GIT_COMMITTER_EMAIL": "dev@shopkit"}
    for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"], ["git", "commit", "-qm", "shopkit"]):
        subprocess.run(cmd, cwd=dest, env=env, check=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=dest, capture_output=True, text=True).stdout.strip()


def run(runs: int, model: str) -> list[dict]:
    os.environ["MEMORY_SHOPKIT_PATH"] = str(repo_path())
    from benchmarks.harness.models import ArmConfiguration, ModelProviderConfig
    from benchmarks.harness.registry import TaskRegistry
    from benchmarks.harness.runner import PairedRunner

    reg = TaskRegistry()
    reg.load_suite(SUITE)
    runner = PairedRunner(provider_config=ModelProviderConfig(name="agy", model=model, temperature=0.0), results_dir=RESULTS)
    tasks = sorted(reg.tasks.values(), key=lambda t: t.id)
    pairs = sorted({t.metadata["pair"] for t in tasks})
    order = ["mem", "forced", "nomem"]
    rows = []
    for it in range(1, runs + 1):
        for pair in pairs:
            # Rotate which variant goes first, so drift over time doesn't favour one.
            rot = order[(it + pairs.index(pair)) % 3:] + order[: (it + pairs.index(pair)) % 3]
            for variant in rot:
                task = next(t for t in tasks if t.metadata["pair"] == pair and t.metadata["variant"] == variant)
                if runner.store.is_run_completed(task.id, it, ArmConfiguration.TREATMENT):
                    r = runner.store.get_run(task.id, it, ArmConfiguration.TREATMENT)
                else:
                    r = runner.execute_arm(task, reg.repositories[task.repository_id], ArmConfiguration.TREATMENT, it)
                captured = None
                mem_file = runner.store.diffs_dir / f"{r.run_id}.memories.json"
                if variant == "nomem" and mem_file.exists():
                    text = " ".join(m["content"] for m in json.loads(mem_file.read_text()))
                    captured = any(w.lower() in text.lower() for w in RULE_WORDS[pair])
                u = r.usage
                rows.append({
                    "pair": pair, "variant": variant, "iteration": it, "status": r.status.value,
                    "passed": r.status.value == "SUCCESS", "memories_recalled": r.memories_recalled,
                    "tokens": (u.prompt_tokens or 0) + (u.completion_tokens or 0), "tool_calls": r.tool_calls_count,
                    "seconds": round(r.duration_seconds), "captured_rule": captured, "run_id": r.run_id,
                })
                print(f"{pair:7} {variant:7} it{it} {r.status.value:22} recalled={r.memories_recalled} tokens={rows[-1]['tokens']} tools={r.tool_calls_count}", flush=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    for v in ("mem", "forced", "nomem"):
        rs = [r for r in rows if r["variant"] == v]
        if not rs:
            continue
        ok = [r for r in rs if r["passed"]]
        out[v] = {
            "runs": len(rs), "passed": len(ok), "memory_injected": sum(1 for r in rs if r["memories_recalled"]),
            "median_tokens": statistics.median(r["tokens"] for r in rs), "median_tool_calls": statistics.median(r["tool_calls"] for r in rs),
            "median_tokens_passed": statistics.median(r["tokens"] for r in ok) if ok else None,
            "per_task_passed": {p: sum(r["passed"] for r in rs if r["pair"] == p) for p in sorted({r["pair"] for r in rs})},
        }
    caps = [r["captured_rule"] for r in rows if r["captured_rule"] is not None]
    out["automatic_capture"] = {"nomem_runs_with_distilled_memories": len(caps), "rule_captured": sum(caps)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--model", default="gemini-3.8-flash-medium")
    ap.add_argument("--json")
    args = ap.parse_args()
    if args.setup:
        print(repo_path(), setup())
        return 0
    rows = run(args.runs, args.model)
    summ = summarize(rows)
    print(json.dumps(summ, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summ, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
