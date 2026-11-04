"""Small embedding models as a meaning-based retriever (public corpus, eval20, vague prompts).

Each repository is split into functions/classes/methods (Python, JS/TS, Go via tree-sitter) plus
one header chunk per file; every chunk is embedded once (cached), and each prompt is matched by
cosine similarity. A file's score is its best chunk. Models are served by a local Ollama.

    python -m benchmarks.embed_eval --models issue-loc-embed,coderankembed,qwen3-embedding:0.6b \
        --json artifacts/embed_eval_2026-11-04.json

Needs numpy (not a Chronos dependency): run it with an environment that has it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import subprocess
import time
import urllib.request
from pathlib import Path

import numpy as np

from benchmarks.corpus import cache_dir, checkout, load_tasks
from benchmarks.corpus.build import LANG_OF_EXT, TEST_RE, _spans

QUERY_PREFIX = {
    "issue-loc-embed": "Represent this query for searching relevant code: ",
    "coderankembed": "Represent this query for searching relevant code: ",
    "qwen3-embedding:0.6b": "Instruct: Given a GitHub issue, retrieve the code that must be changed to fix it\nQuery: ",
}
SKIP_DIRS = re.compile(r"(?:^|/)(?:docs?|examples?|vendor|third_party|node_modules|dist|build|benchmarks?|scripts|\.github)/")
MAX_CHARS = 1800


def chunks(root: Path) -> list[tuple[str, str, str]]:
    """(file, qualified name or '', text) for every function/class and one header per file."""
    files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split()
    out = []
    for rel in files:
        ext = Path(rel).suffix.lower()
        if ext not in LANG_OF_EXT or TEST_RE.search(rel) or SKIP_DIRS.search(rel) or rel.endswith((".d.ts", ".min.js")):
            continue
        p = root / rel
        try:
            src = p.read_bytes()
        except OSError:
            continue
        if len(src) > 600_000:
            continue
        lines = src.decode("utf-8", "replace").splitlines()
        out.append((rel, "", f"{rel}\n" + "\n".join(lines[:60])[:MAX_CHARS]))
        for start, end, name in _spans(src, LANG_OF_EXT[ext], ext):
            body = "\n".join(lines[start - 1:min(end, start + 80)])
            out.append((rel, name, f"{rel} {name}\n{body}"[:MAX_CHARS]))
    return out


def embed(model: str, texts: list[str], batch: int = 32) -> np.ndarray:
    vecs = []
    for i in range(0, len(texts), batch):
        body = json.dumps({"model": model, "input": texts[i:i + batch], "truncate": True, "options": {"num_ctx": 2048}}).encode()
        r = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:11434/api/embed", data=body, headers={"Content-Type": "application/json"}), timeout=600))
        vecs.extend(r["embeddings"])
    a = np.asarray(vecs, dtype=np.float32)
    return a / np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-9)


def index(model: str, task: dict) -> tuple[list[tuple[str, str]], np.ndarray, float]:
    root = checkout(task)
    cache = cache_dir() / "embeddings" / model.replace(":", "_") / f"{task['instance_id']}.npz"
    if cache.exists():
        z = np.load(cache, allow_pickle=True)
        return [tuple(x) for x in z["meta"]], z["vecs"], float(z["seconds"])
    cs = chunks(root)
    t0 = time.perf_counter()
    vecs = embed(model, [c[2] for c in cs])
    secs = time.perf_counter() - t0
    cache.parent.mkdir(parents=True, exist_ok=True)
    np.savez(cache, meta=np.array([(c[0], c[1]) for c in cs], dtype=object), vecs=vecs, seconds=secs)
    return [(c[0], c[1]) for c in cs], vecs, secs


def _sym_hit(name: str, gold: list[str]) -> bool:
    short = name.split(".")[-1]
    return any(name == g or g.endswith("." + short) and g.split(".")[-1] == short or short == g.split(".")[-1] for g in gold)


def rank(model: str, task: dict, prompt: str) -> tuple[list[str], list[str], float, float, int]:
    meta, vecs, idx_s = index(model, task)
    t0 = time.perf_counter()
    q = embed(model, [QUERY_PREFIX.get(model, "") + prompt])[0]
    sims = vecs @ q
    q_ms = (time.perf_counter() - t0) * 1000
    order = np.argsort(-sims)
    files, funcs = [], []
    for i in order:
        f, name = meta[i]
        if f not in files:
            files.append(f)
        if name and len(funcs) < 10:
            funcs.append(name if not funcs or True else name)
            funcs[-1] = name
        if len(files) >= 20 and len(funcs) >= 10:
            break
    return files, funcs, idx_s, q_ms, len(meta)


def rrf(*rankings: list[str], k: int = 60) -> list[str]:
    score: dict[str, float] = {}
    for r in rankings:
        for i, f in enumerate(r):
            score[f] = score.get(f, 0.0) + 1.0 / (k + i + 1)
    return sorted(score, key=lambda f: -score[f])


def score(files: list[str], funcs: list[str], gold_files: list[str], gold_syms: list[str]) -> dict:
    r = next((i + 1 for i, f in enumerate(files) if f in gold_files), None)
    return {"file_top1": r == 1, "file_top3": bool(r and r <= 3), "file_top5": bool(r and r <= 5), "file_rank": r,
            "func_top5": any(_sym_hit(n, gold_syms) for n in funcs[:5]) if gold_syms else None,
            "func_top10": any(_sym_hit(n, gold_syms) for n in funcs[:10]) if gold_syms else None, "files": files[:5]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="issue-loc-embed,coderankembed,qwen3-embedding:0.6b")
    ap.add_argument("--prompt", default="vague", choices=["vague", "issue"])
    ap.add_argument("--baseline", default="artifacts/codescout_eval_2026-11-04.json", help="deterministic arm A rankings for the hybrid")
    ap.add_argument("--tasks", default="eval20", choices=["eval20", "heldout", "all"])
    ap.add_argument("--json")
    args = ap.parse_args()
    base = {}
    if args.baseline and Path(args.baseline).exists():
        base = {r["instance_id"]: r["arms"]["A"]["files"] for r in json.loads(Path(args.baseline).read_text())["rows"]}
    tasks = load_tasks(eval20={"eval20": True, "heldout": False, "all": None}[args.tasks])
    rows = []
    for t in tasks:
        row = {"instance_id": t["instance_id"], "language": t["language"], "gold_files": t["gold_files"], "arms": {}}
        for m in args.models.split(","):
            files, funcs, idx_s, q_ms, n = rank(m, t, t[f"prompt_{args.prompt}"])
            row["arms"][m] = {**score(files, funcs, t["gold_files"], t["gold_symbols"]), "index_s": round(idx_s, 1), "query_ms": round(q_ms), "chunks": n}
            if t["instance_id"] in base:
                row["arms"][m + "+bm25"] = score(rrf(files, base[t["instance_id"]]), funcs, t["gold_files"], t["gold_symbols"])
        if t["instance_id"] in base:
            row["arms"]["bm25 (Chronos today)"] = score(base[t["instance_id"]], [], t["gold_files"], [])
        rows.append(row)
        print(t["instance_id"], {a: (v["file_rank"], v.get("func_top10")) for a, v in row["arms"].items()}, flush=True)
    summary = {}
    for a in rows[0]["arms"]:
        rs = [r["arms"][a] for r in rows if a in r["arms"]]
        fn = [x["func_top10"] for x in rs if x.get("func_top10") is not None]
        summary[a] = {"n": len(rs), "file_top1": sum(x["file_top1"] for x in rs), "file_top3": sum(x["file_top3"] for x in rs),
                      "file_top5": sum(x["file_top5"] for x in rs), "func_top10": f"{sum(fn)}/{len(fn)}" if fn else "-",
                      "median_query_ms": statistics.median(x["query_ms"] for x in rs) if "query_ms" in rs[0] else None,
                      "median_index_s": statistics.median(x["index_s"] for x in rs) if "index_s" in rs[0] else None}
    for a, s in summary.items():
        print(f"{a:34} {s}")
    if args.json:
        Path(args.json).write_text(json.dumps({"prompt": args.prompt, "summary": summary, "rows": rows}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
