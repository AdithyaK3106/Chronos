"""Read structural facts out of codebase-memory-mcp's SQLite graph.

ponytail: the upstream schema is not published, so instead of hardcoding column
names we introspect sqlite_master at runtime and map whatever we find. This is
the ONLY file that knows the upstream schema; if upstream renames a column, fix
it here and nothing else changes.
"""

import os
import sqlite3
from pathlib import Path

# Edge types worth putting in the temporal graph. Structural containment
# (CONTAINS_FILE etc.) is stable and huge; it belongs in upstream's current-state
# graph, not in history.
# ponytail: start narrow, widen when a design partner asks for a type we dropped.
TEMPORAL_EDGE_TYPES = {
    "CALLS", "CALL_REFERENCE", "HTTP_CALLS", "ASYNC_CALLS",
    "IMPORTS", "IMPLEMENTS", "HANDLES", "USES_TYPE",
}

NODE_HINTS = ("node", "symbol", "entit")
EDGE_HINTS = ("edge", "rel", "call")


def default_cache_dir() -> Path:
    return Path(os.environ.get("CBM_CACHE_DIR") or Path.home() / ".cache" / "codebase-memory-mcp")


def find_db(cache_dir: Path | None = None) -> Path | None:
    """Newest .db/.sqlite file in the cache dir, or None."""
    d = cache_dir or default_cache_dir()
    if not d.is_dir():
        return None
    dbs = [p for p in d.rglob("*") if p.suffix in (".db", ".sqlite", ".sqlite3") and p.is_file()]
    return max(dbs, key=lambda p: p.stat().st_mtime) if dbs else None


def _tables(con) -> dict[str, list[str]]:
    out = {}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        out[name] = [r[1] for r in con.execute(f'PRAGMA table_info("{name}")')]
    return out


def _pick(tables: dict[str, list[str]], hints, required) -> tuple[str, dict[str, str]] | None:
    """Find a table whose name matches a hint and whose columns cover `required`.

    required maps our field name -> candidate upstream column names.
    Returns (table, {our_field: actual_column}).
    """
    best = None
    for name, cols in tables.items():
        if not any(h in name.lower() for h in hints):
            continue
        low = {c.lower(): c for c in cols}
        mapping = {}
        for field, candidates in required.items():
            hit = next((low[c] for c in candidates if c in low), None)
            if hit is None:
                mapping = None
                break
            mapping[field] = hit
        if mapping:
            # prefer the table with the most rows-ish signal: more columns = richer
            if best is None or len(cols) > best[2]:
                best = (name, mapping, len(cols))
    return (best[0], best[1]) if best else None


class UpstreamGraph:
    """Adapter over the upstream SQLite graph."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        # read-only: we must never mutate upstream's store (P0-2: it owns current truth)
        self.con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self.con.row_factory = sqlite3.Row
        t = _tables(self.con)
        self.node_tbl = _pick(t, NODE_HINTS, {
            "id": ("id", "node_id", "uuid", "symbol_id"),
            "name": ("name", "symbol", "qualified_name", "fqn"),
        })
        self.edge_tbl = _pick(t, EDGE_HINTS, {
            "src": ("source_id", "src_id", "from_id", "source", "src", "caller_id"),
            "dst": ("target_id", "dst_id", "to_id", "target", "dst", "callee_id"),
            "type": ("type", "edge_type", "kind", "label", "rel_type"),
        })

    @property
    def usable(self) -> bool:
        return self.node_tbl is not None and self.edge_tbl is not None

    def schema_report(self) -> str:
        if self.usable:
            return f"nodes={self.node_tbl[0]}{self.node_tbl[1]} edges={self.edge_tbl[0]}{self.edge_tbl[1]}"
        return f"UNMAPPED tables={list(_tables(self.con))}"

    def _node_extra(self, col_candidates) -> str | None:
        cols = [r[1].lower() for r in self.con.execute(f'PRAGMA table_info("{self.node_tbl[0]}")')]
        return next((c for c in col_candidates if c in cols), None)

    def nodes(self) -> dict[str, dict]:
        tbl, m = self.node_tbl
        path_col = self._node_extra(("file_path", "path", "file"))
        kind_col = self._node_extra(("kind", "type", "label", "node_type"))
        # Upstream declares UNIQUE(project, qualified_name); where it exists it is
        # a better identity than path+name, which collides on nested closures and
        # on folder/file nodes sharing a basename (6 collisions in 379 nodes on a
        # real repo, vs 0 for qualified_name).
        qn_col = self._node_extra(("qualified_name", "fqn", "qname"))
        sel = f'"{m["id"]}" AS id, "{m["name"]}" AS name'
        if path_col:
            sel += f', "{path_col}" AS path'
        if kind_col:
            sel += f', "{kind_col}" AS kind'
        if qn_col:
            sel += f', "{qn_col}" AS qname'
        out = {}
        for r in self.con.execute(f'SELECT {sel} FROM "{tbl}"'):
            d = dict(r)
            out[str(d["id"])] = {
                "name": d.get("name") or str(d["id"]),
                "path": d.get("path") or "",
                "kind": str(d.get("kind") or "Symbol"),
                "qname": d.get("qname") or "",
            }
        return out

    def edges(self) -> list[tuple[str, str, str]]:
        """(src_id, type, dst_id) for temporal-worthy edge types."""
        tbl, m = self.edge_tbl
        rows = self.con.execute(
            f'SELECT "{m["src"]}" AS s, "{m["dst"]}" AS d, "{m["type"]}" AS t FROM "{tbl}"'
        )
        out = []
        for r in rows:
            if r["s"] is None or r["d"] is None:
                continue
            et = str(r["t"] or "").upper()
            if TEMPORAL_EDGE_TYPES and et not in TEMPORAL_EDGE_TYPES:
                continue
            out.append((str(r["s"]), et, str(r["d"])))
        return out

    def coverage(self) -> dict:
        """Parse success/failure counts from upstream's own reporting (P0-1).

        Upstream owns this: it knows which files it failed to parse. Chronos must
        not silently present a partial index as complete, so we surface it rather
        than recompute it. Returns {} if the table isn't present.
        """
        tables = _tables(self.con)
        out: dict = {}

        meta = next((t for t in tables if "coverage_meta" in t.lower()), None)
        if meta:
            cols = tables[meta]
            row = self.con.execute(f'SELECT * FROM "{meta}" LIMIT 1').fetchone()
            if row is not None:
                low = {c.lower(): row[i] for i, c in enumerate(cols)}
                for key, names in (
                    ("recording_status", ("recording_status", "status")),
                    ("index_mode", ("index_mode", "mode")),
                    ("recorded_at", ("recorded_at", "indexed_at", "updated_at")),
                    ("ignored_files", ("ignored_files_total", "ignored_files_stored")),
                ):
                    v = next((low[n] for n in names if n in low), None)
                    if v is not None:
                        out[key] = v

        # One row per file upstream could not fully parse, keyed by kind
        # (parse_partial, etc.). Counting by kind tells a platform engineer which
        # parts of the graph are incomplete without dumping every path.
        cov = next((t for t in tables if t.lower() == "index_coverage"), None)
        if cov and "kind" in [c.lower() for c in tables[cov]]:
            by_kind = {k: n for k, n in self.con.execute(
                f'SELECT kind, count(*) FROM "{cov}" GROUP BY kind')}
            if by_kind:
                out["issues_by_kind"] = by_kind
                out["files_with_issues"] = sum(by_kind.values())
        return out

    def close(self):
        self.con.close()
