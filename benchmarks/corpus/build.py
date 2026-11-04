"""Build benchmarks/corpus/public_v1/tasks.jsonl from SWE-bench Lite and SWE-bench Multilingual.

Run once (needs network, git and the `agy` CLI for the vague prompts); the output is committed.

    python -m benchmarks.corpus.build --data-dir /path/with/lite.json+multi.json

Selection is deterministic: for each repository the instances are sorted by id and spread evenly,
keeping fixes that change 1-3 source files of a language Chronos indexes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from benchmarks.corpus import CORPUS_DIR, TASKS_FILE, checkout

# repo -> (dataset, how many tasks, how many of them in eval20)
QUOTAS: dict[str, tuple[str, int, int]] = {
    "psf/requests": ("lite", 5, 2), "pallets/flask": ("lite", 3, 1), "pytest-dev/pytest": ("lite", 6, 2),
    "pylint-dev/pylint": ("lite", 4, 2), "mwaskom/seaborn": ("lite", 3, 1), "pydata/xarray": ("lite", 2, 1),
    "django/django": ("lite", 2, 1),
    "preactjs/preact": ("multi", 5, 2), "axios/axios": ("multi", 4, 2), "vuejs/core": ("multi", 4, 1),
    "caddyserver/caddy": ("multi", 4, 2), "gin-gonic/gin": ("multi", 4, 2), "prometheus/prometheus": ("multi", 2, 1),
    "gohugoio/hugo": ("multi", 2, 0),
}
LANG_OF_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".ts": "typescript", ".tsx": "typescript", ".go": "go"}
TEST_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|testing|spec|fixtures?)/|(?:^|/)test_[^/]+$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$|conftest\.py$")
DATASETS = {"lite": "princeton-nlp/SWE-bench_Lite", "multi": "SWE-bench/SWE-bench_Multilingual"}


# ----------------------------------------------------------------------------- data
def download(ds: str, dest: Path) -> list[dict]:
    if dest.exists():
        return json.loads(dest.read_text())
    rows = []
    for off in range(0, 300, 50):
        for attempt in range(5):
            try:
                u = f"https://datasets-server.huggingface.co/rows?dataset={ds}&config=default&split=test&offset={off}&length=50"
                rows += [r["row"] for r in json.load(urllib.request.urlopen(u, timeout=60))["rows"]]
                break
            except Exception:  # noqa: BLE001
                time.sleep(3 * (attempt + 1))
    dest.write_text(json.dumps(rows))
    return rows


def patch_files(patch: str) -> list[str]:
    return [m for m in re.findall(r"^diff --git a/(\S+) b/", patch, re.M)]


def changed_old_lines(patch: str) -> dict[str, set[int]]:
    out: dict[str, set[int]] = {}
    cur, old_ln = None, 0
    for line in patch.splitlines():
        if line.startswith("diff --git"):
            m = re.search(r" a/(\S+) b/", line)
            cur = m.group(1) if m else None
            continue
        m = re.match(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
        if m:
            old_ln = int(m.group(1))
            continue
        if cur is None or line.startswith(("---", "+++")):
            continue
        if line.startswith("-"):
            out.setdefault(cur, set()).add(old_ln)
            old_ln += 1
        elif line.startswith("+"):
            out.setdefault(cur, set()).add(max(old_ln - 1, 1))  # an insertion belongs to the code around it
        else:
            old_ln += 1
    return out


# ----------------------------------------------------------------------------- gold symbols (any language)
# The chunker lives in the product now (the embedding retriever uses it); kept under the old names.
from chronos.context.embeddings import DEF_TYPES as _DEF_TYPES  # noqa: E402
from chronos.context.embeddings import code_spans as _spans  # noqa: E402,F401


def gold_symbols(root: Path, changed: dict[str, set[int]]) -> list[str]:
    found: list[str] = []
    for rel, lines in changed.items():
        ext = Path(rel).suffix.lower()
        lang = LANG_OF_EXT.get(ext)
        path = root / rel
        if lang is None or not path.is_file():
            continue
        spans = _spans(path.read_bytes(), lang, ext)
        for ln in sorted(lines):
            inner = [s for s in spans if s[0] <= ln <= s[1]]
            if inner:
                q = max(inner, key=lambda s: (s[0], -s[1]))[2]
                if q not in found:
                    found.append(q)
    return found


# ----------------------------------------------------------------------------- prompts
_FENCE_RE = re.compile(r"```.*?```|~~~.*?~~~", re.S)
_URL_RE = re.compile(r"https?://\S+")
_PATHISH_RE = re.compile(r"(?:[\w.-]+/)+[\w.-]+|[\w-]+\.(?:py|pyi|js|jsx|mjs|ts|tsx|go|json|ya?ml|toml|cfg|ini|html|css|md|rst|txt)\b")
_TRACE_RE = re.compile(r"^\s*(?:File \".*\", line \d+.*|Traceback \(most recent call last\):|at .+\(.+:\d+:\d+\)|\S+\.go:\d+.*|goroutine \d+.*|\s{4,}\S.*)$", re.M)


def issue_prompt(text: str) -> str:
    t = _FENCE_RE.sub(" ", text)
    t = _URL_RE.sub(" ", t)
    t = _TRACE_RE.sub(" ", t)
    t = _PATHISH_RE.sub(" ", t)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t).strip()
    return t[:2500]


def leaks(prompt: str, gold_files: list[str], gold_syms: list[str]) -> list[str]:
    """Gold names that a 'no references' prompt must not contain."""
    low = prompt.lower()
    bad = []
    for f in gold_files:
        stem = Path(f).stem.lower()
        # A stem that is a plain word ("blueprints", "reader") is part of the problem's vocabulary;
        # one that looks like an identifier ("http_adapter", "routerGroup") is a reference.
        identifier_like = bool(re.search(r"[_\d]|[a-z][A-Z]", Path(f).stem))
        if Path(f).name.lower() in low or (identifier_like and len(stem) >= 5 and re.search(rf"\b{re.escape(stem)}\b", low)):
            bad.append(Path(f).name)
    for s in gold_syms:
        short = s.split(".")[-1]
        if len(short) >= 4 and re.search(rf"\b{re.escape(short)}\b", prompt):
            bad.append(short)
    if _PATHISH_RE.search(prompt) or re.search(r"`[^`]+`", prompt) or re.search(r"\b\w+\(\)", prompt):
        bad.append("path/code reference")
    return bad


VAGUE_INSTRUCTIONS = """Rewrite this GitHub issue as a short request a developer might type to a coding assistant.
Rules:
- 1 to 3 plain sentences describing the wrong behaviour (and the expected behaviour if clear).
- Do NOT mention any file, folder, path, module, function, method, class, variable or API name.
- No code, no backticks, no stack traces, no URLs, no version numbers.
- You may name the product or library itself (e.g. "requests", "Flask", "Gin").
Output only the request text.

ISSUE ({repo}):
{issue}
"""


def vague_prompt(repo: str, issue: str, gold_files: list[str], gold_syms: list[str], model: str = "gemini-3.8-flash-low") -> tuple[str | None, list[str]]:
    extra = ""
    for _ in range(3):
        prompt = VAGUE_INSTRUCTIONS.format(repo=repo, issue=issue[:6000]) + extra
        with tempfile.TemporaryDirectory() as empty:
            try:
                r = subprocess.run(["agy", f"--model={model}", "--disable-slash-commands", "--print", prompt],
                                   cwd=empty, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
            except subprocess.TimeoutExpired:
                continue
        text = r.stdout.strip().strip('"').strip()
        if not text:
            continue
        bad = leaks(text, gold_files, gold_syms)
        if not bad:
            return text, []
        extra = f"\nYour previous answer mentioned {', '.join(sorted(set(bad)))}. Do not mention those or any code names."
    return None, bad


# ----------------------------------------------------------------------------- build
def select(rows: list[dict], repo: str, n: int) -> list[dict]:
    cands = []
    for r in sorted((r for r in rows if r["repo"] == repo), key=lambda r: r["instance_id"]):
        src = [f for f in patch_files(r["patch"]) if Path(f).suffix.lower() in LANG_OF_EXT and not TEST_RE.search(f)]
        if 1 <= len(src) <= 3 and len(r["problem_statement"]) > 80:
            cands.append(r)
    if len(cands) <= n:
        return cands
    step = len(cands) / n
    return [cands[int(i * step)] for i in range(n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--only", help="comma-separated repos (debug)")
    ap.add_argument("--recompute-gold", action="store_true", help="only recompute gold_symbols of existing tasks")
    args = ap.parse_args()
    if args.recompute_gold:
        tasks = [json.loads(line) for line in TASKS_FILE.read_text().splitlines() if line.strip()]
        for t in tasks:
            changed = {f: ls for f, ls in changed_old_lines(t["patch"]).items() if f in t["gold_files"]}
            new = gold_symbols(checkout(t), changed)
            if new != t["gold_symbols"]:
                print(t["instance_id"], t["gold_symbols"], "->", new)
            t["gold_symbols"] = new
        TASKS_FILE.write_text("".join(json.dumps(t) + "\n" for t in tasks))
        return 0
    data = {k: download(ds, args.data_dir / f"{k}.json") for k, ds in DATASETS.items()}
    existing = {}
    if TASKS_FILE.exists():
        existing = {json.loads(line)["instance_id"]: json.loads(line) for line in TASKS_FILE.read_text().splitlines() if line.strip()}
    out = []
    for repo, (ds, n, n_eval) in QUOTAS.items():
        if args.only and repo not in args.only.split(","):
            continue
        for i, r in enumerate(select(data[ds], repo, n)):
            iid = r["instance_id"]
            if iid in existing and existing[iid].get("prompt_vague"):
                out.append(existing[iid])
                print("kept", iid, flush=True)
                continue
            src = [f for f in patch_files(r["patch"]) if Path(f).suffix.lower() in LANG_OF_EXT and not TEST_RE.search(f)]
            task = {"instance_id": iid, "repo": repo, "base_commit": r["base_commit"], "source": DATASETS[ds]}
            root = checkout(task)
            changed = {f: ls for f, ls in changed_old_lines(r["patch"]).items() if f in src}
            syms = gold_symbols(root, changed)
            vague, bad = vague_prompt(repo, r["problem_statement"], src, syms)
            ftp = r.get("FAIL_TO_PASS")
            task.update({
                "language": LANG_OF_EXT[Path(src[0]).suffix.lower()],
                "eval20": i < n_eval,
                "prompt_vague": vague,
                "prompt_issue": issue_prompt(r["problem_statement"]),
                "gold_files": src,
                "gold_symbols": syms,
                "patch": r["patch"],
                "test_patch": r.get("test_patch", ""),
                "fail_to_pass": json.loads(ftp) if isinstance(ftp, str) and ftp.startswith("[") else ftp,
                "created_at": r.get("created_at"),
            })
            out.append(task)
            print(f"{iid:45} {task['language']:10} files={len(src)} syms={len(syms)} vague={'ok' if vague else 'FAILED ' + str(bad)}", flush=True)
            CORPUS_DIR.mkdir(parents=True, exist_ok=True)
            TASKS_FILE.write_text("".join(json.dumps(t) + "\n" for t in out + [v for k, v in existing.items() if k not in {t['instance_id'] for t in out}]))
    TASKS_FILE.write_text("".join(json.dumps(t) + "\n" for t in out))
    print(f"{len(out)} tasks, eval20={sum(t['eval20'] for t in out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
