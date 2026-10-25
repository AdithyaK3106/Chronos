"""Optional Kùzu property graph backend (portability gate)."""

import os
from pathlib import Path
from typing import Any
import kuzu

from chronos.core.models import ASTEdge, ASTNode
from chronos.context.graph import GraphStore, _normalize_iso

class KuzuGraphStore(GraphStore):
    """Kùzu property graph backend."""

    def __init__(self, db_dir: str | Path):
        self.db_dir = Path(db_dir).resolve()
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.kuzu_path = str(self.db_dir / "graph.kuzu")
        self.db = kuzu.Database(self.kuzu_path)
        self.conn = kuzu.Connection(self.db)
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            self.conn.execute(
                "CREATE NODE TABLE CodeNode(id STRING, name STRING, path STRING, kind STRING, PRIMARY KEY (id))"
            )
            self.conn.execute(
                "CREATE REL TABLE RELATES_TO(FROM CodeNode TO CodeNode, type STRING, valid_at STRING, invalid_at STRING)"
            )
        except Exception:
            pass # Tables already exist

    def ingest(self, nodes: list[ASTNode], edges: list[ASTEdge]) -> None:
        for n in nodes:
            try:
                self.conn.execute(
                    "MERGE (c:CodeNode {id: $id}) ON MATCH SET c.name = $name, c.path = $path, c.kind = $kind "
                    "ON CREATE SET c.name = $name, c.path = $path, c.kind = $kind",
                    {"id": n.id, "name": n.name, "path": n.path, "kind": n.kind},
                )
            except Exception:
                pass

        for e in edges:
            inv = e.invalid_at or ""
            try:
                self.conn.execute(
                    """
                    MATCH (a:CodeNode {id: $src}), (b:CodeNode {id: $dst})
                    CREATE (a)-[r:RELATES_TO {type: $type, valid_at: $valid_at, invalid_at: $invalid_at}]->(b)
                    """,
                    {"src": e.src, "dst": e.dst, "type": e.type, "valid_at": e.valid_at, "invalid_at": inv},
                )
            except Exception:
                pass

    def as_of_callers(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        t = _normalize_iso(as_of)
        res = self.conn.execute(
            """
            MATCH (src:CodeNode)-[r:RELATES_TO]->(dst:CodeNode)
            WHERE dst.name = $name
              AND r.valid_at <= $t
              AND (r.invalid_at = '' OR r.invalid_at > $t)
            RETURN src.id AS id, src.name AS name, src.path AS path, src.kind AS kind, r.type AS type
            """,
            {"name": symbol_name, "t": t},
        )
        out = []
        while res.has_next():
            row = res.get_next()
            out.append({"id": row[0], "name": row[1], "path": row[2], "kind": row[3], "type": row[4]})
        return out

    def as_of_callees(self, symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
        t = _normalize_iso(as_of)
        res = self.conn.execute(
            """
            MATCH (src:CodeNode)-[r:RELATES_TO]->(dst:CodeNode)
            WHERE src.name = $name
              AND r.valid_at <= $t
              AND (r.invalid_at = '' OR r.invalid_at > $t)
            RETURN dst.name AS name, r.type AS type
            """,
            {"name": symbol_name, "t": t},
        )
        out = []
        while res.has_next():
            row = res.get_next()
            out.append({"name": row[0], "type": row[1]})
        return out

    def as_of_impact(self, symbol_name: str, as_of: str | None = None, max_depth: int = 3) -> list[dict[str, Any]]:
        res = self.conn.execute(
            f"""
            MATCH (src:CodeNode)-[r:RELATES_TO*1..{max_depth}]->(dst:CodeNode)
            WHERE dst.name = $name
            RETURN DISTINCT src.id AS id, src.name AS name, src.path AS path, src.kind AS kind
            """,
            {"name": symbol_name},
        )
        out = []
        while res.has_next():
            row = res.get_next()
            out.append({"id": row[0], "name": row[1], "path": row[2], "kind": row[3]})
        return out

    def as_of_diff(self, t1: str, t2: str) -> dict[str, Any]:
        return {"t1": t1, "t2": t2, "status": "use_sqlite_for_diff"}
