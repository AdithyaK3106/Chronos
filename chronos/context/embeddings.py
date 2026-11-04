"""Optional embedding retriever: finds code by meaning when the prompt names no files or symbols.

Off by default. Enable in `.chronos/config.json`:

    {"retrieval": {"embeddings": {"enabled": true, "model": "issue-loc-embed",
                                  "endpoint": "http://127.0.0.1:11434"}}}

Why it exists: on vague prompts (no file or function names) over public repositories, BM25 put the
file that needed changing in its top 3 for 5/30 held-out tasks; `issue-loc-embed` did for 17/30
(artifacts/embed_eval_heldout_2026-11-05.json; 13/20 vs 8/20 on eval20).

How it works:
- Each source file is split into one header chunk plus one chunk per function, class and method
  (tree-sitter; Python, JS/TS, Go), the same chunks the evaluation used.
- Chunks are embedded by a local Ollama server (`POST /api/embed`) and stored in the repository's
  SQLite database as normalised float32 blobs, keyed by file content hash, so a re-index only
  embeds files that changed and drops files that were deleted.
- A query is embedded once; a file's score is its best chunk's cosine similarity.
- No numpy: dot products use `math.sumprod` (C speed on Python 3.12+), else a Python loop.

Any failure (server down, model missing, empty index) returns no results, and the lexical
resolver carries on as before.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
import urllib.request
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

LANG_OF_EXT = {".py": "python", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
               ".ts": "typescript", ".tsx": "typescript", ".go": "go"}
TEST_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|testing|spec|fixtures?)/|(?:^|/)test_[^/]+$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$|conftest\.py$")
SKIP_DIRS = re.compile(r"(?:^|/)(?:docs?|examples?|vendor|third_party|node_modules|dist|build|benchmarks?|scripts|\.github)/")
MAX_CHARS = 1800
MAX_FILE_BYTES = 600_000
QUERY_PREFIX = {
    "issue-loc-embed": "Represent this query for searching relevant code: ",
    "coderankembed": "Represent this query for searching relevant code: ",
    "qwen3-embedding:0.6b": "Instruct: Given a GitHub issue, retrieve the code that must be changed to fix it\nQuery: ",
}

DEF_TYPES = {
    "python": {"function_definition": "name", "class_definition": "name"},
    "javascript": {"function_declaration": "name", "method_definition": "name", "class_declaration": "name", "generator_function_declaration": "name",
                   "function_expression": "name", "function": "name", "class": "name"},
    "typescript": {"function_declaration": "name", "method_definition": "name", "class_declaration": "name", "interface_declaration": "name", "abstract_class_declaration": "name",
                   "function_expression": "name", "function": "name"},
    "go": {"function_declaration": "name", "method_declaration": "name", "type_spec": "name"},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS embedding_files (
    model TEXT NOT NULL, path TEXT NOT NULL, file_hash TEXT NOT NULL,
    PRIMARY KEY (model, path)
);
CREATE TABLE IF NOT EXISTS embedding_chunks (
    model TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL, vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embedding_chunks_model ON embedding_chunks(model, path);
"""


# ----------------------------------------------------------------------------- chunking
def code_spans(src: bytes, lang: str, ext: str) -> list[tuple[int, int, str]]:
    """(start_line, end_line, qualified name) of every function, class and method (1-based)."""
    from chronos.context.indexer import get_tree_sitter_parser

    parser = get_tree_sitter_parser("tsx" if ext == ".tsx" else lang)
    if parser is None:
        return []
    tree = parser.parse(src)
    types = DEF_TYPES[lang]
    spans: list[tuple[int, int, str]] = []

    def name_of(node) -> str | None:
        n = node.child_by_field_name(types[node.type])
        return src[n.start_byte:n.end_byte].decode("utf-8", "replace") if n is not None else None

    def walk(node, prefix: str) -> None:
        for ch in node.children:
            nm = name_of(ch) if ch.type in types else None
            # `const f = (...) => {}` / `const f = function () {}`
            if nm is None and ch.type in ("lexical_declaration", "variable_declaration") and lang in ("javascript", "typescript"):
                for d in ch.children:
                    if d.type == "variable_declarator":
                        val = d.child_by_field_name("value")
                        if val is not None and val.type in ("arrow_function", "function_expression", "function"):
                            n = d.child_by_field_name("name")
                            if n is not None:
                                q = prefix + src[n.start_byte:n.end_byte].decode()
                                spans.append((ch.start_point[0] + 1, ch.end_point[0] + 1, q))
                                walk(val, q + ".")
                continue
            if nm is not None:
                if ch.type == "method_declaration" and lang == "go":
                    recv = ch.child_by_field_name("receiver")
                    rt = re.findall(r"\*?(\w+)\s*\)?$", src[recv.start_byte:recv.end_byte].decode()) if recv is not None else []
                    q = f"{rt[0]}.{nm}" if rt else nm
                else:
                    q = prefix + nm
                spans.append((ch.start_point[0] + 1, ch.end_point[0] + 1, q))
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    walk(tree.root_node, "")
    return spans


def eligible(rel: str) -> bool:
    """Source files worth embedding: supported language, not a test, doc, vendored or build file."""
    return (Path(rel).suffix.lower() in LANG_OF_EXT and not TEST_RE.search(rel) and not SKIP_DIRS.search(rel)
            and not rel.endswith((".d.ts", ".min.js")))


def file_chunks(rel: str, src: bytes) -> list[tuple[str, str]]:
    """(name, text) chunks for one file: a header chunk (name '') plus one per definition."""
    ext = Path(rel).suffix.lower()
    lines = src.decode("utf-8", "replace").splitlines()
    out = [("", (f"{rel}\n" + "\n".join(lines[:60]))[:MAX_CHARS])]
    for start, end, name in code_spans(src, LANG_OF_EXT[ext], ext):
        body = "\n".join(lines[start - 1:min(end, start + 80)])
        out.append((name, f"{rel} {name}\n{body}"[:MAX_CHARS]))
    return out


# ----------------------------------------------------------------------------- vectors
def _normalise(v: Iterable[float]) -> array:
    a = array("f", v)
    n = math.sqrt(_dot(a, a)) or 1e-9
    return array("f", (x / n for x in a))


if hasattr(math, "sumprod"):
    def _dot(a: Any, b: Any) -> float:
        return math.sumprod(a, b)
else:  # Python < 3.12
    def _dot(a: Any, b: Any) -> float:
        return sum(x * y for x, y in zip(a, b))


def ollama_embed(endpoint: str, model: str, texts: list[str], timeout: float = 120.0, batch: int = 32) -> list[array]:
    """Normalised embeddings from Ollama's /api/embed. Raises on any HTTP or format error."""
    out: list[array] = []
    url = endpoint.rstrip("/") + "/api/embed"
    for i in range(0, len(texts), batch):
        body = json.dumps({"model": model, "input": texts[i:i + batch], "truncate": True, "options": {"num_ctx": 2048}}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        vecs = data.get("embeddings")
        if not isinstance(vecs, list) or len(vecs) != len(texts[i:i + batch]):
            raise ValueError(f"unexpected /api/embed response from {url}")
        out.extend(_normalise(v) for v in vecs)
    return out


# ----------------------------------------------------------------------------- index
@dataclass
class FileHit:
    path: str
    score: float      # best chunk's cosine similarity
    best_chunk: str   # qualified name of that chunk ('' = the file header)


class EmbeddingIndex:
    """Per-repository chunk vectors in the repo's SQLite database, one set per model."""

    def __init__(self, con: sqlite3.Connection, root: str | Path, model: str = "issue-loc-embed",
                 endpoint: str = "http://127.0.0.1:11434", timeout_seconds: float = 10.0, embed_fn: Any = None):
        self.con = con
        self.root = Path(root).resolve()
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout_seconds
        self._embed = embed_fn or (lambda texts, timeout: ollama_embed(self.endpoint, self.model, texts, timeout=timeout))
        con.executescript(SCHEMA)

    def _tracked_files(self) -> list[str]:
        try:
            r = subprocess.run(["git", "-C", str(self.root), "ls-files"], capture_output=True, text=True, timeout=30)
            files = r.stdout.split("\n") if r.returncode == 0 else []
        except Exception:  # noqa: BLE001
            files = []
        if not files or files == [""]:
            files = [p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file() and ".git" not in p.parts]
        return [f for f in files if f and eligible(f)]

    def update(self, files: list[str] | None = None) -> dict[str, int]:
        """Embed new and changed files, drop deleted ones. `files` limits the work to those paths."""
        tracked = self._tracked_files() if files is None else [f for f in files if eligible(f)]
        known = dict(self.con.execute("SELECT path, file_hash FROM embedding_files WHERE model = ?", (self.model,)).fetchall())
        stats = {"embedded_files": 0, "chunks": 0, "removed_files": 0, "unchanged_files": 0}
        for rel in tracked:
            p = self.root / rel
            try:
                src = p.read_bytes()
            except OSError:
                continue
            if len(src) > MAX_FILE_BYTES:
                continue
            digest = hashlib.sha1(src).hexdigest()
            if known.get(rel) == digest:
                stats["unchanged_files"] += 1
                continue
            chunks = file_chunks(rel, src)
            vecs = self._embed([c[1] for c in chunks], max(self.timeout, 120.0))
            with self.con:
                self.con.execute("DELETE FROM embedding_chunks WHERE model = ? AND path = ?", (self.model, rel))
                self.con.executemany(
                    "INSERT INTO embedding_chunks (model, path, name, vec) VALUES (?, ?, ?, ?)",
                    [(self.model, rel, name, v.tobytes()) for (name, _), v in zip(chunks, vecs)],
                )
                self.con.execute(
                    "INSERT INTO embedding_files (model, path, file_hash) VALUES (?, ?, ?) "
                    "ON CONFLICT(model, path) DO UPDATE SET file_hash = excluded.file_hash",
                    (self.model, rel, digest),
                )
            stats["embedded_files"] += 1
            stats["chunks"] += len(chunks)
        if files is None:
            gone = set(known) - set(tracked)
            with self.con:
                for rel in gone:
                    self.con.execute("DELETE FROM embedding_chunks WHERE model = ? AND path = ?", (self.model, rel))
                    self.con.execute("DELETE FROM embedding_files WHERE model = ? AND path = ?", (self.model, rel))
            stats["removed_files"] = len(gone)
        return stats

    def indexed_files(self) -> int:
        return self.con.execute("SELECT count(*) FROM embedding_files WHERE model = ?", (self.model,)).fetchone()[0]

    def search(self, prompt: str, top_files: int = 20) -> list[FileHit]:
        """Files ranked by their best chunk's similarity to the prompt. [] on any failure."""
        try:
            rows = self.con.execute("SELECT path, name, vec FROM embedding_chunks WHERE model = ?", (self.model,)).fetchall()
            if not rows:
                return []
            q = self._embed([QUERY_PREFIX.get(self.model, "") + prompt], self.timeout)[0]
        except Exception:  # noqa: BLE001 - the lexical resolver still runs
            return []
        best: dict[str, tuple[float, str]] = {}
        for path, name, blob in rows:
            v = array("f")
            v.frombytes(blob)
            if len(v) != len(q):
                continue
            s = _dot(q, v)
            if s > best.get(path, (-2.0, ""))[0]:
                best[path] = (s, name)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:top_files]
        return [FileHit(path, s, name) for path, (s, name) in ranked]


def rrf(*rankings: list[str], k: int = 60) -> list[str]:
    """Reciprocal rank fusion of several ranked lists (each item scores 1/(k + rank))."""
    score: dict[str, float] = {}
    for r in rankings:
        for i, f in enumerate(r):
            score[f] = score.get(f, 0.0) + 1.0 / (k + i + 1)
    return sorted(score, key=lambda f: -score[f])


def index_from_config(con: sqlite3.Connection, root: str | Path, cfg: Any) -> EmbeddingIndex | None:
    """An EmbeddingIndex if `cfg` (EmbeddingsConfig) is enabled, else None."""
    if cfg is None or not getattr(cfg, "enabled", False):
        return None
    return EmbeddingIndex(con, root, model=cfg.model, endpoint=cfg.endpoint, timeout_seconds=cfg.timeout_seconds)
