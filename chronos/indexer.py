"""Run the vendored codebase-memory-mcp indexer ourselves.

Alternative input path to upstream.py's "read whatever DB is already there".
Same output schema; upstream.py is untouched and still used to read the result.

We build the vendored C from source (vendor/codebase-memory-mcp, MIT, pinned as a
submodule) rather than downloading a release binary, so nothing external is
required at runtime.

Why subprocess and not ctypes: the indexer's entry point is cbm_pipeline_run(),
which needs a constructed pipeline, an initialized store, an arena allocator and a
thread pool -- the per-file extraction API in internal/cbm/ does not do cross-file
type resolution, which is the whole reason we want their indexer. The pipeline
already writes the nodes/edges SQLite that upstream.py reads, so going through the
CLI keeps their pipeline intact and costs one process spawn.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .upstream import UpstreamGraph

EXTENSION_TO_LANGUAGE = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".kt": "kotlin",
    ".swift": "swift",
}

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR = REPO_ROOT / "vendor" / "codebase-memory-mcp"
BINARY = VENDOR / "build" / "c" / "codebase-memory-mcp"
BUILD_CMD = "python -m chronos.build_cbm"


def binary_path() -> Path | None:
    """The built indexer, or None if it hasn't been built yet."""
    for p in (BINARY, BINARY.with_suffix(".exe")):
        if p.is_file():
            return p
    return None


def _require_binary() -> Path:
    p = binary_path()
    if p is None:
        raise FileNotFoundError(
            f"vendored indexer not built at {BINARY}\n"
            f"  build it with:  {BUILD_CMD}\n"
            f"  (needs a C toolchain; on Windows, MSYS2 gcc)"
        )
    return p


def cache_dir() -> Path:
    d = Path(os.environ.get("CBM_CACHE_DIR") or Path.home() / ".cache" / "codebase-memory-mcp")
    d.mkdir(parents=True, exist_ok=True)
    return d


def index_repo(repo_path: str, mode: str = "fast", timeout: int = 1800) -> list[dict]:
    """Index a repo and return its structural graph.

    Returns a list of dicts, one per node and one per edge:
      {"kind": "node", "id", "name", "path", "node_kind"}
      {"kind": "edge", "src", "type", "dst"}

    Both reference the same ids, so they feed sync.Syncer directly. `mode` is
    passed through to the indexer: fast (default) skips similarity/semantic edges,
    which Chronos does not use; full/moderate compute them.
    """
    binary = _require_binary()
    repo = str(Path(repo_path).resolve())
    env = {**os.environ, "CBM_CACHE_DIR": str(cache_dir())}

    proc = subprocess.run(
        [str(binary), "cli", "--json", "index_repository", "--repo-path", repo, "--mode", mode],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"indexer failed (exit {proc.returncode}): {proc.stderr[-2000:]}")

    result = _parse_result(proc.stdout)
    if result.get("status") not in (None, "indexed"):
        raise RuntimeError(f"indexer reported status={result.get('status')}: {result}")

    db = cache_dir() / f"{result['project']}.db"
    if not db.is_file():
        raise FileNotFoundError(f"indexer reported success but no db at {db}")

    g = UpstreamGraph(db)
    if not g.usable:
        g.close()
        raise RuntimeError(f"could not map indexer output schema: {g.schema_report()}")
    try:
        nodes, edges = g.nodes(), g.edges()
    finally:
        g.close()

    out: list[dict] = [
        {"kind": "node", "id": nid, "name": n["name"], "path": n["path"],
         "node_kind": n["kind"], "qname": n.get("qname", "")}
        for nid, n in nodes.items()
    ]
    out += [{"kind": "edge", "src": s, "type": t, "dst": d} for s, t, d in edges]
    return out


def node_language(path: str) -> str:
    """Language from a node's file extension.

    The upstream indexer does not emit a language field, so Wedge 4's
    per-language rule scoping had nothing to filter on and every rule applied to
    every file. Extension is a coarse but honest signal: 'unknown' when we cannot
    tell, never None, so a caller can distinguish "not a source file" from
    "field missing".
    """
    ext = os.path.splitext(path or "")[1].lower()
    return EXTENSION_TO_LANGUAGE.get(ext, "unknown")


def index_repo_graph(repo_path: str, **kw) -> tuple[dict[str, dict], list[tuple[str, str, str]]]:
    """index_repo() in the (nodes, edges) shape Syncer.sync() takes."""
    rows = index_repo(repo_path, **kw)
    # qname must survive: node_identity() prefers it, and dropping it silently
    # downgrades every indexed node to the colliding path+name identity.
    nodes = {r["id"]: {"name": r["name"], "path": r["path"], "kind": r["node_kind"],
                       "qname": r.get("qname", ""),
                       "language": node_language(r.get("path", ""))}
             for r in rows if r["kind"] == "node"}
    edges = [(r["src"], r["type"], r["dst"]) for r in rows if r["kind"] == "edge"]
    return nodes, edges


def _parse_result(stdout: str) -> dict:
    """Pull the result object out of the CLI's output.

    The binary logs `level=...` lines and deprecation notices to the same stream,
    so we scan for the JSON envelope rather than parsing the whole thing.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        # `cli --json` wraps the payload in an MCP-style envelope.
        inner = obj.get("structuredContent")
        if isinstance(inner, dict) and "project" in inner:
            return inner
        if "project" in obj:
            return obj
    raise RuntimeError(f"could not parse indexer output:\n{stdout[-2000:]}")


def toolchain_report() -> dict:
    """What `chronos doctor` needs to explain a missing build."""
    p = binary_path()
    return {
        "vendored": (VENDOR / "Makefile.cbm").is_file(),
        "binary": str(p) if p else None,
        "make": shutil.which("make") or shutil.which("mingw32-make"),
        "cc": shutil.which("cc") or shutil.which("gcc"),
        "build_cmd": BUILD_CMD,
    }
