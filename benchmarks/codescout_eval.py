"""Does CodeScout improve context retrieval on prompts with no file names? (public corpus, eval20)

Arms, all on the same prompt (`prompt_vague` by default):
  A  deterministic   Chronos resolver (AST index + BM25), what the prompt hook uses today
  B  slm_oneshot     the existing CodeScout integration (one JSON-only call, first 150 files alphabetically)
  C  scout_agent     CodeScout as trained: read-only terminal search loop (chronos.context.codescout_agent)
  D  fusion          C's files first, then A's

Metrics: gold file ranked 1st / in top 3 / in top 5, gold function found, seconds per prompt.

    python -m benchmarks.codescout_eval --json artifacts/codescout_eval.json [--prompt issue] [--arms A,C,D]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path

from benchmarks.corpus import checkout, load_tasks


def _sym_match(cand: str, gold: list[str]) -> bool:
    c = cand.split("::")[-1]
    return any(c == g or c.endswith("." + g.split(".")[-1]) and g.split(".")[-1] == c.split(".")[-1] or g.endswith("." + c) or c == g.split(".")[-1] for g in gold)


def _rel(p: str, root: Path) -> str:
    try:
        return Path(p).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(p).removeprefix("./")


def deterministic(prompt: str, root: Path) -> tuple[list[str], list[str]]:
    from chronos.context.session import ChronosSessionBootstrap
    from chronos.core.db import get_connection

    con = get_connection(root / ".chronos" / "chronos.db")
    try:
        res = ChronosSessionBootstrap(repo_path=root, con=con).bootstrap_session(
            task_prompt=prompt, agent_id="codescout_eval", session_id="eval", token_budget=1200,
            refresh_index=False, acquire_leases=False,
        )
    finally:
        con.close()
    cands = sorted(res.get("candidates") or [], key=lambda c: -c.score)
    files: list[str] = []
    for c in cands:
        f = _rel(c.path, root)
        if f not in files:
            files.append(f)
    syms = [c.name for c in cands[:8] if c.kind != "File"]
    return files, syms


def ensure_index(root: Path) -> float:
    from chronos.context.graph import SQLiteGraphStore
    from chronos.context.indexer import index_repository
    from chronos.core.db import get_connection

    marker = root / ".chronos" / "eval_indexed"
    if marker.exists():
        return 0.0
    t0 = time.perf_counter()
    con = get_connection(root / ".chronos" / "chronos.db")
    try:
        nodes, edges = index_repository(root)
        SQLiteGraphStore(con).sync_repository(nodes, edges)
    finally:
        con.close()
    marker.write_text("1")
    return time.perf_counter() - t0


def slm_oneshot(prompt: str, root: Path, endpoint: str, model: str) -> tuple[list[str], list[str]]:
    from chronos.core.config import SLMConfig
    from chronos.context.slm_resolver import LocalSLMResolver

    r = LocalSLMResolver(root, config=SLMConfig(enabled=True, backend="ollama", endpoint=endpoint, model=model, timeout_seconds=120))
    res = r.resolve_prompt(prompt)
    return list(res.suspected_files or []), list(res.suspected_symbols or [])


def score(files: list[str], syms: list[str], gold_files: list[str], gold_syms: list[str]) -> dict:
    rank = next((i + 1 for i, f in enumerate(files) if f in gold_files), None)
    return {
        "file_top1": rank == 1, "file_top3": bool(rank and rank <= 3), "file_top5": bool(rank and rank <= 5),
        "function": any(_sym_match(s, gold_syms) for s in syms) if gold_syms else None,
        "files": files[:5], "symbols": syms[:5],
    }


def run(arms: list[str], prompt_key: str, endpoint: str, model: str, limit: int | None = None, tasks: str = "eval20") -> list[dict]:
    from chronos.context.codescout_agent import scout

    rows = []
    for t in load_tasks(eval20={"eval20": True, "heldout": False, "all": None}[tasks])[:limit]:
        prompt = t[f"prompt_{prompt_key}"]
        if not prompt:
            continue
        root = checkout(t)
        idx_s = ensure_index(root)
        row = {"instance_id": t["instance_id"], "language": t["language"], "gold_files": t["gold_files"], "gold_symbols": t["gold_symbols"], "index_s": round(idx_s, 1), "arms": {}}
        det = None
        if "A" in arms or "D" in arms:
            t0 = time.perf_counter()
            det = deterministic(prompt, root)
            row["arms"]["A"] = {**score(*det, t["gold_files"], t["gold_symbols"]), "seconds": round(time.perf_counter() - t0, 2)}
        if "B" in arms:
            t0 = time.perf_counter()
            b = slm_oneshot(prompt, root, endpoint, model)
            row["arms"]["B"] = {**score(*b, t["gold_files"], t["gold_symbols"]), "seconds": round(time.perf_counter() - t0, 2)}
        if "C" in arms or "D" in arms:
            sc = scout(prompt, root, endpoint=endpoint, model=model)
            row["arms"]["C"] = {**score(sc.files, sc.symbols, t["gold_files"], t["gold_symbols"]), "seconds": round(sc.seconds, 1),
                                "turns": sc.turns, "refused": len(sc.refused), "error": sc.error}
            if det is not None:
                files = sc.files + [f for f in det[0] if f not in sc.files]
                syms = sc.symbols + [s for s in det[1] if s not in sc.symbols]
                row["arms"]["D"] = {**score(files, syms, t["gold_files"], t["gold_symbols"]), "seconds": round(sc.seconds + row["arms"]["A"]["seconds"], 1)}
        rows.append(row)
        print(t["instance_id"], {a: (v["file_top1"], v["file_top3"], v["function"], v["seconds"]) for a, v in row["arms"].items()}, flush=True)
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    arms = sorted({a for r in rows for a in r["arms"]})
    for a in arms:
        rs = [r["arms"][a] for r in rows if a in r["arms"]]
        fn = [x["function"] for x in rs if x["function"] is not None]
        out[a] = {
            "n": len(rs),
            "file_top1": sum(x["file_top1"] for x in rs), "file_top3": sum(x["file_top3"] for x in rs), "file_top5": sum(x["file_top5"] for x in rs),
            "function": f"{sum(fn)}/{len(fn)}",
            "median_s": round(statistics.median(x["seconds"] for x in rs), 1), "max_s": round(max(x["seconds"] for x in rs), 1),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="A,B,C,D")
    ap.add_argument("--prompt", default="vague", choices=["vague", "issue"])
    ap.add_argument("--endpoint", default="http://127.0.0.1:11434")
    ap.add_argument("--model", default="codescout-1.7b")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tasks", default="eval20", choices=["eval20", "heldout", "all"])
    ap.add_argument("--json")
    args = ap.parse_args()
    rows = run(args.arms.split(","), args.prompt, args.endpoint, args.model, args.limit, args.tasks)
    summ = summarize(rows)
    for a, s in summ.items():
        print(a, s)
    if args.json:
        Path(args.json).write_text(json.dumps({"prompt": args.prompt, "summary": summ, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
