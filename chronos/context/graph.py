"""Bi-temporal AST knowledge graph storage engine (Wedge 1).

Features:
- Dual-engine architecture:
  1. SQLiteWALGraphStore: Universal, zero-dependency, sub-millisecond recursive CTE queries.
  2. KuzuGraphStore: Optional embedded property graph engine.
- Bi-temporal valid-time windowing:
  A relationship is visible as-of timestamp `t` iff:
  `valid_at <= t AND (invalid_at IS NULL OR invalid_at > t)`
"""

import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chronos.core.models import ASTEdge, ASTNode, utc_now_iso

def _normalize_iso(dt_or_str: str | datetime | None) -> str:
    if dt_or_str is None:
        return utc_now_iso()
    if isinstance(dt_or_str, datetime):
        return dt_or_str.isoformat()
    return dt_or_str

class GraphStore(ABC):
    """Abstract interface for bi-temporal AST graph queries."""

    @abstractmethod
    def ingest(self, nodes: list[ASTNode], edges: list[ASTEdge]) -> None:
        """Ingest nodes and temporal edges."""
        pass

    @abstractmethod
    def as_of_callers(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        """Return symbols that call `symbol_name` as of given timestamp."""
        pass

    @abstractmethod
    def as_of_callees(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        """Return symbols called by `symbol_name` as of given timestamp."""
        pass

    @abstractmethod
    def as_of_impact(self, symbol_name: str, as_of: str | None = None, max_depth: int = 3) -> list[dict[str, Any]]:
        """Transitive callers (downstream blast radius) up to max_depth as of given timestamp."""
        pass

    @abstractmethod
    def as_of_diff(self, t1: str, t2: str) -> dict[str, Any]:
        """Compute structural edge differences between two timestamps."""
        pass

    def get_nodes_by_name(self, symbol_name: str) -> list[dict[str, Any]]:
        """Return code nodes matching name or id."""
        return []

    def query_symbols_lexical(self, query_terms: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Search code_nodes using lexical full-text matching."""
        return []

    def update_file(self, file_path: str | Path, valid_at: str | None = None) -> tuple[int, int]:
        """Incrementally update AST nodes and bi-temporal edges for a modified file."""
        return (0, 0)

class SQLiteGraphStore(GraphStore):
    """SQLite WAL implementation using recursive CTEs for transitive graph traversal."""

    def __init__(self, con: sqlite3.Connection):
        self.con = con

    def ingest(self, nodes: list[ASTNode], edges: list[ASTEdge]) -> None:
        self.con.execute("BEGIN IMMEDIATE")
        try:
            for n in nodes:
                self.con.execute(
                    """
                    INSERT INTO code_nodes (id, name, path, kind, language, signature, docstring)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, path=excluded.path, kind=excluded.kind,
                        signature=excluded.signature, docstring=excluded.docstring
                    """,
                    (n.id, n.name, n.path, n.kind, n.language, n.signature, n.docstring),
                )

            for e in edges:
                self.con.execute(
                    """
                    INSERT INTO code_edges (src, dst, type, valid_at, invalid_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(src, dst, type, valid_at) DO UPDATE SET
                        invalid_at=excluded.invalid_at
                    """,
                    (e.src, e.dst, e.type, e.valid_at, e.invalid_at),
                )
            self.con.execute("COMMIT")
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except Exception:
                pass
            raise

    def as_of_callers(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        t = _normalize_iso(as_of)
        # Direct callers: edges where dst resolves to symbol_name or qualified node ID
        rows = self.con.execute(
            """
            SELECT n.id, n.name, n.path, n.kind, e.type, e.valid_at
            FROM code_edges e
            JOIN code_nodes n ON e.src = n.id
            WHERE (e.dst = ? OR e.dst LIKE '%::' || ? || '::%' OR e.dst LIKE '%::' || ?)
              AND e.valid_at <= ?
              AND (e.invalid_at IS NULL OR e.invalid_at > ?)
            ORDER BY n.name ASC
            """,
            (symbol_name, symbol_name, symbol_name, t, t),
        ).fetchall()
        return [dict(r) for r in rows]

    def as_of_callees(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        t = _normalize_iso(as_of)
        rows = self.con.execute(
            """
            SELECT COALESCE(n2.name, e.dst) AS name,
                   e.dst AS target_id,
                   n2.path AS target_path,
                   e.type,
                   e.valid_at
            FROM code_edges e
            JOIN code_nodes n ON e.src = n.id
            LEFT JOIN code_nodes n2 ON e.dst = n2.id
            WHERE (n.name = ? OR n.id = ?)
              AND e.valid_at <= ?
              AND (e.invalid_at IS NULL OR e.invalid_at > ?)
            ORDER BY name ASC
            """,
            (symbol_name, symbol_name, t, t),
        ).fetchall()
        return [dict(r) for r in rows]

    def as_of_impact(self, symbol_name: str, as_of: str | None = None, max_depth: int = 3) -> list[dict[str, Any]]:
        t = _normalize_iso(as_of)
        rows = self.con.execute(
            """
            WITH RECURSIVE impact_tree(node_id, depth) AS (
                SELECT id, 0 FROM code_nodes WHERE name = ? OR id = ?
                UNION
                SELECT e.src, it.depth + 1
                FROM code_edges e
                JOIN impact_tree it ON (e.dst = it.node_id OR e.dst LIKE '%::' || it.node_id || '::%')
                WHERE it.depth < ?
                  AND e.valid_at <= ?
                  AND (e.invalid_at IS NULL OR e.invalid_at > ?)
            )
            SELECT DISTINCT n.id, n.name, n.path, n.kind, it.depth
            FROM impact_tree it
            JOIN code_nodes n ON it.node_id = n.id
            WHERE it.depth > 0
            ORDER BY it.depth ASC, n.name ASC
            """,
            (symbol_name, symbol_name, max_depth, t, t),
        ).fetchall()
        return [dict(r) for r in rows]

    def as_of_diff(self, t1: str, t2: str) -> dict[str, Any]:
        """Compute edges active at t2 that were not active at t1 (added), and vice versa (removed)."""
        norm_t1 = _normalize_iso(t1)
        norm_t2 = _normalize_iso(t2)

        q = """
            SELECT src, dst, type FROM code_edges
            WHERE valid_at <= ? AND (invalid_at IS NULL OR invalid_at > ?)
        """
        edges_t1 = set((r[0], r[1], r[2]) for r in self.con.execute(q, (norm_t1, norm_t1)).fetchall())
        edges_t2 = set((r[0], r[1], r[2]) for r in self.con.execute(q, (norm_t2, norm_t2)).fetchall())

        added = [{"src": s, "dst": d, "type": t} for s, d, t in (edges_t2 - edges_t1)]
        removed = [{"src": s, "dst": d, "type": t} for s, d, t in (edges_t1 - edges_t2)]
        unchanged = len(edges_t1 & edges_t2)

        return {
            "t1": norm_t1,
            "t2": norm_t2,
            "added_edges": added,
            "removed_edges": removed,
            "unchanged_count": unchanged,
        }

    def get_nodes_by_name(self, symbol_name: str) -> list[dict[str, Any]]:
        """Return code nodes matching name or id."""
        rows = self.con.execute(
            """
            SELECT id, name, path, kind, language, signature, docstring
            FROM code_nodes
            WHERE name = ? OR id = ?
            ORDER BY id ASC
            """,
            (symbol_name, symbol_name),
        ).fetchall()
        return [dict(r) for r in rows]

    def query_symbols_lexical(self, query_terms: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Search code_nodes using lexical full-text matching."""
        if not query_terms:
            return []
        # Sanitize FTS5 query terms: wrap alphanumeric tokens in quotes to prevent FTS syntax injection
        clean_terms = ['"' + t.replace('"', '""') + '"' for t in query_terms if t and (t.isalnum() or '_' in t)]
        if not clean_terms:
            return []
        fts_query = " OR ".join(clean_terms)
        try:
            rows = self.con.execute(
                """
                SELECT n.id, n.name, n.path, n.kind, n.signature, n.docstring,
                       bm25(code_nodes_fts) AS bm25_rank
                FROM code_nodes_fts f
                JOIN code_nodes n ON f.id = n.id
                WHERE code_nodes_fts MATCH ?
                ORDER BY bm25_rank ASC
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
            return [dict(r) for r in rows]
        except (sqlite3.OperationalError, Exception):
            # Graceful fallback to LIKE query if FTS table query encounters any issue
            valid_terms = [t for t in query_terms if t][:4]
            if not valid_terms:
                return []
            placeholders = " OR ".join(["n.name LIKE ? OR n.path LIKE ?" for _ in valid_terms])
            params = []
            for t in valid_terms:
                params.extend([f"%{t}%", f"%{t}%"])
            params.append(limit)
            rows = self.con.execute(
                f"SELECT id, name, path, kind, signature, docstring, 0.0 AS bm25_rank FROM code_nodes n WHERE {placeholders} LIMIT ?",
                params,
            ).fetchall()
            return [dict(r) for r in rows]

    def update_file(self, file_path: str | Path, valid_at: str | None = None) -> tuple[int, int]:
        """Incrementally update AST nodes and bi-temporal edges for a modified file."""
        from chronos.context.indexer import extract_file_ast
        from chronos.context.linker import CrossFileLinker
        t = _normalize_iso(valid_at)
        p = Path(file_path).resolve()
        norm_path = p.as_posix()

        # 1. Invalidate active outgoing edges from previous version of this file
        pattern = f"{norm_path}::%"
        self.con.execute(
            """
            UPDATE code_edges
            SET invalid_at = ?
            WHERE (src LIKE ? OR src = ?) AND invalid_at IS NULL
            """,
            (t, pattern, norm_path),
        )

        # 2. Extract new nodes, edges, and imports from disk
        nodes, edges, imports = extract_file_ast(p, valid_at=t)

        # 3. Disambiguate cross-file edges against the graph store
        curr = p.parent
        repo_root = curr
        while curr != curr.parent:
            if (curr / ".git").is_dir() or (curr / ".chronos").is_dir() or (curr / "pyproject.toml").is_file():
                repo_root = curr
                break
            curr = curr.parent

        linker = CrossFileLinker(repo_root)
        linked_nodes, linked_edges = linker.link_file(p, nodes, edges, imports, graph_store=self, valid_at=t)

        # 4. Upsert nodes and insert new edges with valid_at = t
        if linked_nodes or linked_edges:
            self.ingest(linked_nodes, linked_edges)

        return len(linked_nodes), len(linked_edges)

def create_graph_store(con: sqlite3.Connection, backend: str = "sqlite", kuzu_dir: str | Path | None = None) -> GraphStore:
    """Factory creating the appropriate GraphStore with portability fallback."""
    if backend == "kuzu":
        try:
            import kuzu  # Check availability
            from chronos.context.kuzu_backend import KuzuGraphStore
            return KuzuGraphStore(kuzu_dir or ".chronos/kuzu")
        except (ImportError, Exception):
            pass # Fall back to SQLite
    return SQLiteGraphStore(con)
