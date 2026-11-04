"""Retrieval-only benchmark: does Chronos's context point at the code the fix changes?

For every efficacy task with a reference fix (acceptance/<repo>/reference/<task>.patch), in a fresh
worktree at the task's commit:
  1. index the repository the way the Claude Code hook does;
  2. resolve the task prompt with the hook's own path (`ChronosSessionBootstrap.bootstrap_session`,
     no `target_files`, no memories);
  3. score the result against the *gold* files and symbols the reference patch changes.

Each task is scored twice: with the prompt as written, and with file paths removed ("no paths"),
because all 20 prompts name their target file and so never test localization.

No model calls. Typical run: well under a minute per repository.

    python -m benchmarks.retrieval [--task ID ...] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from benchmarks.harness.models import TaskSuiteType
from benchmarks.harness.registry import TaskRegistry
from benchmarks.harness.workspace import WorktreeManager

SUITE = Path(__file__).parent / "suites" / "enterprise_24"
MIN_INJECT_SCORE = 12.0  # chronos.integrations.claude_code.MIN_INJECT_SCORE default

_PATH_RE = re.compile(
    r"(?:\b(?:in|of|from|to|inside)\s+)?`?(?:[\w.-]+/)*[\w.-]+\.(?:py|json|ya?ml|ts|tsx|js|md|toml|cfg|ini|txt)`?"
)
_DEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)")


def strip_paths(prompt: str) -> str:
    """Remove file paths (and a leading 'in'/'of') so retrieval has to find the code itself."""
    out = _PATH_RE.sub("", prompt)
    return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.;:])", r"\1", out)).strip()


def gold_from_patch(patch: str) -> tuple[list[str], list[str]]:
    """Files a unified diff changes, and the functions/classes it touches (hunk headers + changed defs)."""
    files: list[str] = []
    symbols: list[str] = []
    for line in patch.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip()
            if p != "/dev/null":
                files.append(p[2:] if p.startswith("b/") else p)
        elif line.startswith("@@"):
            ctx = line.split("@@", 2)[-1]
            m = _DEF_RE.match(ctx)
            if m:
                symbols.append(m.group(1))
        elif line[:1] in "+-" and not line.startswith(("+++", "---")):
            m = _DEF_RE.match(line[1:])
            if m:
                symbols.append(m.group(1))
    return list(dict.fromkeys(files)), list(dict.fromkeys(symbols))


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def changed_old_lines(patch: str) -> dict[str, set[int]]:
    """Per file: line numbers in the ORIGINAL file that the patch changes or inserts at."""
    out: dict[str, set[int]] = {}
    cur: str | None = None
    old_no = 0
    for line in patch.splitlines():
        if line.startswith("--- "):
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            cur = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            if cur:
                out.setdefault(cur, set())
            continue
        m = _HUNK_RE.match(line)
        if m:
            old_no = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("-"):
            out[cur].add(old_no)
            old_no += 1
        elif line.startswith("+"):
            out[cur].add(max(old_no - 1, 1))  # insertion point: the line before
        else:
            old_no += 1
    return out


def gold_symbols_from_source(root: Path, changed: dict[str, set[int]]) -> list[str]:
    """Innermost qualified def/class (`Class.method`) around each changed line, for Python files."""
    import ast

    found: list[str] = []
    for rel, lines in changed.items():
        path = root / rel
        if not rel.endswith(".py") or not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        spans: list[tuple[int, int, str]] = []

        def walk(node, prefix: str) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    q = f"{prefix}{child.name}"
                    start = min([d.lineno for d in child.decorator_list] + [child.lineno])
                    spans.append((start, child.end_lineno or child.lineno, q))
                    walk(child, q + ".")

        walk(tree, "")
        for ln in sorted(lines):
            inner = [s for s in spans if s[0] <= ln <= s[1]]
            if inner:
                q = max(inner, key=lambda s: s[0])[2]  # latest start = innermost
                if q not in found:
                    found.append(q)
    return found


def _sym_match(cand: str, gold: list[str]) -> bool:
    """`Class.method` matches gold `Class.method`, or a gold class `Class` (a method of it)."""
    return any(cand == g or cand.startswith(g + ".") for g in gold)


@dataclass
class Score:
    variant: str
    injected: bool
    top_score: float
    file_rank: int | None              # 1-based rank of the first gold file among candidate files
    files_found_top5: int
    files_total: int
    gold_file_in_envelope: bool
    symbol_rank: int | None            # 1-based rank of the first gold symbol among candidates
    gold_symbol_in_envelope: bool
    top_candidates: list[str] = field(default_factory=list)
    ms: float = 0.0


def _rel(path: str, root: Path) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else p.as_posix()
    except ValueError:
        return p.as_posix()


def score_resolution(res: dict, root: Path, gold_files: list[str], gold_symbols: list[str], variant: str, ms: float) -> Score:
    cands = sorted(res.get("candidates") or [], key=lambda c: -c.score)
    cand_files: list[str] = []
    for c in cands:
        f = _rel(c.path, root)
        if f not in cand_files:
            cand_files.append(f)
    file_rank = next((i + 1 for i, f in enumerate(cand_files) if f in gold_files), None)
    sym_rank = next((i + 1 for i, c in enumerate(cands) if c.kind != "File" and _sym_match(c.name, gold_symbols) and _rel(c.path, root) in gold_files), None)
    env_files = {_rel(f, root) for f in res.get("resolved_files") or []}
    env_syms = {str(s).split("::")[1] if "::" in str(s) else str(s) for s in (res.get("symbols") or [])}
    return Score(
        variant=variant,
        injected=bool(cands) and cands[0].score >= MIN_INJECT_SCORE,
        top_score=round(cands[0].score, 1) if cands else 0.0,
        file_rank=file_rank,
        files_found_top5=sum(1 for f in gold_files if f in cand_files[:5]),
        files_total=len(gold_files),
        gold_file_in_envelope=bool(env_files & set(gold_files)),
        symbol_rank=sym_rank,
        gold_symbol_in_envelope=any(_sym_match(x, gold_symbols) for x in env_syms),
        top_candidates=[f"{_rel(c.path, root)}::{c.name} ({c.score:.0f})" for c in cands[:3]],
        ms=round(ms, 1),
    )


def run(task_ids: list[str] | None = None) -> list[dict]:
    from chronos.context.graph import SQLiteGraphStore
    from chronos.context.indexer import index_repository
    from chronos.context.session import ChronosSessionBootstrap
    from chronos.core.db import get_connection

    reg = TaskRegistry()
    reg.load_suite(SUITE)
    tasks = [t for t in reg.tasks.values() if t.suite == TaskSuiteType.EFFICACY and (not task_ids or t.id in task_ids)]
    results = []
    for task in sorted(tasks, key=lambda t: t.id):
        patch_file = SUITE / "acceptance" / task.repository_id / "reference" / f"{task.id}.patch"
        if not patch_file.is_file():
            continue
        patch_text = patch_file.read_text(encoding="utf-8")
        gold_files, header_symbols = gold_from_patch(patch_text)
        repo = reg.repositories[task.repository_id]
        with WorktreeManager(repo.path, commit=task.starting_commit or repo.baseline_commit) as mgr:
            root = Path(mgr.workspace_path)
            gold_symbols = gold_symbols_from_source(root, changed_old_lines(patch_text)) or header_symbols
            con = get_connection(root / ".chronos" / "chronos.db")
            try:
                nodes, edges = index_repository(root)
                SQLiteGraphStore(con).sync_repository(nodes, edges)
                boot = ChronosSessionBootstrap(repo_path=root, con=con)
                scores = []
                for variant, prompt in (("as written", task.prompt), ("no paths", strip_paths(task.prompt))):
                    t0 = time.perf_counter()
                    res = boot.bootstrap_session(
                        task_prompt=prompt, agent_id="retrieval_bench", session_id=f"{task.id}-{variant}",
                        token_budget=1200, refresh_index=False, acquire_leases=False,
                    )
                    scores.append(score_resolution(res, root, gold_files, gold_symbols, variant, (time.perf_counter() - t0) * 1000))
            finally:
                con.close()
        results.append({
            "task_id": task.id, "gold_files": gold_files, "gold_symbols": gold_symbols,
            "no_paths_prompt": strip_paths(task.prompt), "scores": [asdict(s) for s in scores],
        })
    return results


def summarize(results: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for variant in ("as written", "no paths"):
        rows = [s for r in results for s in r["scores"] if s["variant"] == variant]
        n = len(rows)
        if not n:
            continue
        ranks = [s["file_rank"] for s in rows if s["file_rank"]]
        out[variant] = {
            "tasks": n,
            "injected": sum(s["injected"] for s in rows),
            "file_hit@1": sum(1 for s in rows if s["file_rank"] == 1),
            "file_hit@3": sum(1 for s in rows if s["file_rank"] and s["file_rank"] <= 3),
            "file_hit@5": sum(1 for s in rows if s["file_rank"] and s["file_rank"] <= 5),
            "all_gold_files@5": sum(1 for s in rows if s["files_found_top5"] == s["files_total"]),
            "gold_file_in_envelope": sum(s["gold_file_in_envelope"] for s in rows),
            "symbol_hit@3": sum(1 for s in rows if s["symbol_rank"] and s["symbol_rank"] <= 3),
            "gold_symbol_in_envelope": sum(s["gold_symbol_in_envelope"] for s in rows),
            "median_file_rank": statistics.median(ranks) if ranks else None,
            "median_ms": statistics.median(s["ms"] for s in rows),
        }
    return out


def markdown(results: list[dict]) -> str:
    summ = summarize(results)
    lines = ["| Measure | " + " | ".join(summ) + " |", "|---|" + "---|" * len(summ)]
    labels = {
        "injected": "hook would inject (top score >= 12)",
        "file_hit@1": "gold file ranked 1st",
        "file_hit@3": "gold file in top 3",
        "file_hit@5": "gold file in top 5",
        "all_gold_files@5": "all gold files in top 5",
        "gold_file_in_envelope": "gold file in injected context",
        "symbol_hit@3": "gold function in top 3",
        "gold_symbol_in_envelope": "gold function in injected context",
        "median_ms": "median resolve time (ms)",
    }
    for key, label in labels.items():
        cells = []
        for v in summ.values():
            val = v[key]
            cells.append(f"{val}" if key == "median_ms" else f"{val}/{v['tasks']}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "| Task | gold | as written: file rank / fn rank | no paths: file rank / fn rank | no paths: top candidate |", "|---|---|---|---|---|"]
    for r in results:
        a, b = r["scores"]
        lines.append(
            f"| {r['task_id']} | {', '.join(r['gold_symbols'][:2]) or '-'} | {a['file_rank'] or '-'} / {a['symbol_rank'] or '-'} "
            f"| {b['file_rank'] or '-'} / {b['symbol_rank'] or '-'} | {(b['top_candidates'] or ['-'])[0]} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--task", action="append", help="only these task ids")
    ap.add_argument("--json", help="write full results to this file")
    args = ap.parse_args(argv)
    results = run(args.task)
    if not results:
        print("No tasks with reference patches found.", file=sys.stderr)
        return 1
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summarize(results), "tasks": results}, indent=2), encoding="utf-8")
    print(markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
