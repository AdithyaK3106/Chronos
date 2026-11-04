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

    def get_nodes_by_name(self, symbol_name: str, include_members: bool = False) -> list[dict[str, Any]]:
        """Return code nodes matching name or id (and `Class.<name>` members if include_members)."""
        return []

    def query_symbols_lexical(self, query_terms: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Search code_nodes using lexical full-text matching."""
        return []

    def update_file(self, file_path: str | Path, valid_at: str | None = None) -> tuple[int, int]:
        """Incrementally update AST nodes and bi-temporal edges for a modified file."""
        return (0, 0)

    def sync_repository(
        self, nodes: list[ASTNode], edges: list[ASTEdge], valid_at: str | None = None
    ) -> dict[str, int]:
        """Reconcile the store against a full-repository index snapshot.

        Backends without reconciliation support fall back to a plain ingest.
        """
        self.ingest(nodes, edges)
        return {"nodes_removed": 0, "edges_added": len(edges), "edges_invalidated": 0, "duplicates_removed": 0}

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
                    INSERT INTO code_nodes (id, name, path, kind, language, signature, docstring, terms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        name=excluded.name, path=excluded.path, kind=excluded.kind,
                        signature=excluded.signature, docstring=excluded.docstring, terms=excluded.terms
                    """,
                    (n.id, n.name, n.path, n.kind, n.language, n.signature, n.docstring, n.terms),
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

    def get_nodes_by_name(self, symbol_name: str, include_members: bool = False) -> list[dict[str, Any]]:
        """Return code nodes matching name or id, exact matches first.

        With include_members, `as_of_impact` also matches methods stored as `Class.as_of_impact`
        (the resolver uses this; call-edge linking does not, so its resolution is unchanged).
        """
        member = "OR (instr(name, '.') > 0 AND substr(name, -length(?) - 1) = '.' || ?)" if include_members else ""
        params = (symbol_name, symbol_name) + ((symbol_name, symbol_name) if include_members else ()) + (symbol_name,)
        rows = self.con.execute(
            f"""
            SELECT id, name, path, kind, language, signature, docstring
            FROM code_nodes
            WHERE name = ? OR id = ? {member}
            ORDER BY (name = ?) DESC, id ASC
            """,
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def query_symbols_lexical(self, query_terms: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Search code_nodes using lexical full-text matching."""
        if not query_terms:
            return []
        # Sanitize FTS5 query terms: wrap alphanumeric tokens in quotes to prevent FTS syntax injection
        clean_terms = ['"' + t.replace('"', '""') + '"' for t in query_terms if t and (t.isalnum() or '_' in t)]
        # Crude stemming on top of the porter tokenizer: `expiry` -> `expir*` also finds
        # `expires`/`expired`/`expiration`, which porter stems differently.
        clean_terms += [
            '"' + t[: max(5, len(t) - 2)].lower() + '"*' for t in query_terms
            if t and t.isalpha() and len(t) >= 6
        ]
        if not clean_terms:
            return []
        fts_query = " OR ".join(clean_terms)
        try:
            rows = self.con.execute(
                """
                SELECT n.id, n.name, n.path, n.kind, n.signature, n.docstring,
                       bm25(code_nodes_fts, 0.0, 10.0, 2.0, 3.0, 2.0, 1.0) AS bm25_rank
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

        # 4. Drop nodes for symbols that no longer exist in this file
        keep_ids = [n.id for n in linked_nodes if n.path == norm_path]
        placeholders = ",".join("?" for _ in keep_ids)
        stale_sql = "DELETE FROM code_nodes WHERE path = ?"
        if keep_ids:
            stale_sql += f" AND id NOT IN ({placeholders})"
        self.con.execute(stale_sql, (norm_path, *keep_ids))

        # 5. Upsert nodes and insert new edges with valid_at = t
        if linked_nodes or linked_edges:
            self.ingest(linked_nodes, linked_edges)

        return len(linked_nodes), len(linked_edges)

    def sync_repository(
        self, nodes: list[ASTNode], edges: list[ASTEdge], valid_at: str | None = None
    ) -> dict[str, int]:
        """Reconcile the store against a full-repository index snapshot.

        Unlike ``ingest`` (which appends), this keeps the edge history bi-temporal:
        - edges already active and still present are left untouched (original valid_at kept),
        - active edges missing from the snapshot are closed with invalid_at = t,
        - edges new in the snapshot are inserted,
        - duplicate active rows for the same (src, dst, type) are removed,
        - nodes absent from the snapshot are deleted.

        ``nodes``/``edges`` must describe the WHOLE repository; anything not included is
        treated as deleted.
        """
        t = _normalize_iso(valid_at)
        new_node_ids = {n.id for n in nodes}
        new_edges = {(e.src, e.dst, e.type): e for e in edges}
        stats = {"nodes_removed": 0, "edges_added": 0, "edges_invalidated": 0, "duplicates_removed": 0}

        self.con.execute("BEGIN IMMEDIATE")
        try:
            # 1. Upsert current nodes
            self.con.executemany(
                """
                INSERT INTO code_nodes (id, name, path, kind, language, signature, docstring, terms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name, path=excluded.path, kind=excluded.kind,
                    language=excluded.language, signature=excluded.signature,
                    docstring=excluded.docstring, terms=excluded.terms
                """,
                [(n.id, n.name, n.path, n.kind, n.language, n.signature, n.docstring, n.terms) for n in nodes],
            )

            # 2. Remove nodes that no longer exist
            existing_ids = {r[0] for r in self.con.execute("SELECT id FROM code_nodes").fetchall()}
            stale_ids = existing_ids - new_node_ids
            if stale_ids:
                self.con.executemany("DELETE FROM code_nodes WHERE id = ?", [(i,) for i in stale_ids])
            stats["nodes_removed"] = len(stale_ids)

            # 3. Reconcile active edges (oldest first, so the original valid_at survives)
            active_rows = self.con.execute(
                "SELECT rowid, src, dst, type FROM code_edges WHERE invalid_at IS NULL ORDER BY valid_at ASC"
            ).fetchall()
            seen: set[tuple[str, str, str]] = set()
            duplicate_rowids: list[tuple[int]] = []
            retire_rowids: list[tuple[str, int]] = []
            for row in active_rows:
                key = (row[1], row[2], row[3])
                if key in seen:
                    duplicate_rowids.append((row[0],))
                    continue
                seen.add(key)
                if key not in new_edges:
                    retire_rowids.append((t, row[0]))

            if duplicate_rowids:
                self.con.executemany("DELETE FROM code_edges WHERE rowid = ?", duplicate_rowids)
            if retire_rowids:
                self.con.executemany("UPDATE code_edges SET invalid_at = ? WHERE rowid = ?", retire_rowids)
            stats["duplicates_removed"] = len(duplicate_rowids)
            stats["edges_invalidated"] = len(retire_rowids)

            # 4. Insert edges that are new in this snapshot
            to_insert = [
                (e.src, e.dst, e.type, e.valid_at or t)
                for key, e in new_edges.items()
                if key not in seen
            ]
            if to_insert:
                self.con.executemany(
                    "INSERT OR IGNORE INTO code_edges (src, dst, type, valid_at, invalid_at) VALUES (?, ?, ?, ?, NULL)",
                    to_insert,
                )
            stats["edges_added"] = len(to_insert)

            self.con.execute("COMMIT")
        except Exception:
            try:
                self.con.execute("ROLLBACK")
            except Exception:
                pass
            raise
        return stats

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
