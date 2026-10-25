import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
import kuzu

# Generate representative repository graph:
# 2,000 functions/classes across 50 modules, 6,000 call edges across 3 timestamps (T0, T1, T2)
NODES_COUNT = 2000
EDGES_COUNT = 6000

T0 = "2026-01-01T00:00:00Z"
T1 = "2026-06-01T00:00:00Z"
T2 = "2026-09-01T00:00:00Z"

def generate_graph_data():
    nodes = []
    for i in range(NODES_COUNT):
        mod_idx = i % 50
        nodes.append({
            "id": f"node_{i}",
            "name": f"fn_{i}",
            "path": f"src/module_{mod_idx}.py",
            "kind": "Function" if i % 4 != 0 else "Class"
        })
    
    edges = []
    # Create call chains and some high fan-in hubs (like logging, utils, db.get_connection)
    for i in range(EDGES_COUNT):
        src = f"node_{i % NODES_COUNT}"
        # Hubs: 0..10 are common utility functions
        if i % 3 == 0:
            dst = f"node_{i % 10}"
        else:
            dst = f"node_{(i * 7 + 13) % NODES_COUNT}"
        
        # Valid-time temporal windows:
        # 70% alive across whole time
        # 15% deleted at T1
        # 15% introduced at T1
        if i % 10 < 7:
            valid_at = T0
            invalid_at = None
        elif i % 10 < 8:
            valid_at = T0
            invalid_at = T1
        else:
            valid_at = T1
            invalid_at = None
        
        edges.append({
            "src": src,
            "dst": dst,
            "type": "CALLS",
            "valid_at": valid_at,
            "invalid_at": invalid_at
        })
    return nodes, edges

def test_sqlite(nodes, edges, queries):
    db_file = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(db_file)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    
    t0 = time.perf_counter()
    con.execute("""
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY,
            name TEXT,
            path TEXT,
            kind TEXT
        )
    """)
    con.execute("""
        CREATE TABLE edges (
            src TEXT,
            dst TEXT,
            type TEXT,
            valid_at TEXT,
            invalid_at TEXT
        )
    """)
    con.execute("CREATE INDEX idx_edges_dst ON edges(dst, valid_at, invalid_at)")
    con.execute("CREATE INDEX idx_edges_src ON edges(src, valid_at, invalid_at)")
    con.execute("CREATE INDEX idx_nodes_name ON nodes(name)")

    con.executemany("INSERT INTO nodes VALUES (?,?,?,?)", [(n["id"], n["name"], n["path"], n["kind"]) for n in nodes])
    con.executemany("INSERT INTO edges VALUES (?,?,?,?,?)", [(e["src"], e["dst"], e["type"], e["valid_at"], e["invalid_at"]) for e in edges])
    con.commit()
    sqlite_write_time = (time.perf_counter() - t0) * 1000
    sqlite_size = os.path.getsize(db_file)

    # 1. As-of callers (direct 1-hop)
    as_of_time = "2026-03-01T00:00:00Z"
    t_start = time.perf_counter()
    for sym in queries:
        con.execute("""
            SELECT n.name, n.path
            FROM edges e
            JOIN nodes n ON e.src = n.id
            JOIN nodes target ON e.dst = target.id
            WHERE target.name = ?
              AND e.valid_at <= ?
              AND (e.invalid_at IS NULL OR e.invalid_at > ?)
        """, (sym, as_of_time, as_of_time)).fetchall()
    sqlite_callers_time = (time.perf_counter() - t_start) * 1000 / len(queries)

    # 2. As-of Impact (Transitive callers up to 4 hops using recursive CTE)
    t_start = time.perf_counter()
    for sym in queries[:20]:
        con.execute("""
            WITH RECURSIVE impact_tree(node_id, depth) AS (
                SELECT id, 0 FROM nodes WHERE name = ?
                UNION
                SELECT e.src, it.depth + 1
                FROM edges e
                JOIN impact_tree it ON e.dst = it.node_id
                WHERE it.depth < 4
                  AND e.valid_at <= ?
                  AND (e.invalid_at IS NULL OR e.invalid_at > ?)
            )
            SELECT DISTINCT n.name, n.path, it.depth
            FROM impact_tree it
            JOIN nodes n ON it.node_id = n.id
            WHERE it.depth > 0
        """, (sym, as_of_time, as_of_time)).fetchall()
    sqlite_impact_time = (time.perf_counter() - t_start) * 1000 / 20

    con.close()
    if os.path.exists(db_file):
        os.remove(db_file)
    for extra in [db_file + "-wal", db_file + "-shm"]:
        if os.path.exists(extra):
            os.remove(extra)
    
    return {
        "write_ms": sqlite_write_time,
        "size_bytes": sqlite_size,
        "callers_ms": sqlite_callers_time,
        "impact_ms": sqlite_impact_time
    }

def test_kuzu(nodes, edges, queries):
    tmp_dir = tempfile.mkdtemp()
    kuzu_path = os.path.join(tmp_dir, "graph.kuzu")
    db = kuzu.Database(kuzu_path)
    conn = kuzu.Connection(db)

    t0 = time.perf_counter()
    conn.execute("CREATE NODE TABLE CodeNode(id STRING, name STRING, path STRING, kind STRING, PRIMARY KEY (id))")
    conn.execute("CREATE REL TABLE RELATES_TO(FROM CodeNode TO CodeNode, type STRING, valid_at STRING, invalid_at STRING)")

    for n in nodes:
        conn.execute("CREATE (c:CodeNode {id: $id, name: $name, path: $path, kind: $kind})", {
            "id": n["id"], "name": n["name"], "path": n["path"], "kind": n["kind"]
        })
    for e in edges:
        inv = e["invalid_at"] or ""
        conn.execute("""
            MATCH (a:CodeNode {id: $src}), (b:CodeNode {id: $dst})
            CREATE (a)-[r:RELATES_TO {type: $type, valid_at: $valid_at, invalid_at: $invalid_at}]->(b)
        """, {
            "src": e["src"], "dst": e["dst"], "type": e["type"], "valid_at": e["valid_at"], "invalid_at": inv
        })
    kuzu_write_time = (time.perf_counter() - t0) * 1000

    # Calculate dir size
    kuzu_size = sum(os.path.getsize(os.path.join(dirpath, f)) for dirpath, _, filenames in os.walk(tmp_dir) for f in filenames)

    as_of_time = "2026-03-01T00:00:00Z"
    t_start = time.perf_counter()
    for sym in queries:
        conn.execute("""
            MATCH (src:CodeNode)-[r:RELATES_TO]->(dst:CodeNode)
            WHERE dst.name = $name
              AND r.valid_at <= $t
              AND (r.invalid_at = '' OR r.invalid_at > $t)
            RETURN src.name, src.path
        """, {"name": sym, "t": as_of_time})
    kuzu_callers_time = (time.perf_counter() - t_start) * 1000 / len(queries)

    # Transitive callers (1..4 hops)
    t_start = time.perf_counter()
    for sym in queries[:20]:
        conn.execute("""
            MATCH (src:CodeNode)-[r:RELATES_TO*1..4]->(dst:CodeNode)
            WHERE dst.name = $name
            RETURN DISTINCT src.name, src.path
        """, {"name": sym})
    kuzu_impact_time = (time.perf_counter() - t_start) * 1000 / 20

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return {
        "write_ms": kuzu_write_time,
        "size_bytes": kuzu_size,
        "callers_ms": kuzu_callers_time,
        "impact_ms": kuzu_impact_time
    }

if __name__ == "__main__":
    nodes, edges = generate_graph_data()
    queries = [f"fn_{i}" for i in range(100)] # 100 sample symbol queries
    print("Running benchmark...")
    sq = test_sqlite(nodes, edges, queries)
    kz = test_kuzu(nodes, edges, queries)
    print("\n--- BENCHMARK RESULTS ---")
    print(f"Dataset: {NODES_COUNT} nodes, {EDGES_COUNT} edges, 100 query evaluations")
    print(f"SQLite WAL: Write={sq['write_ms']:.1f}ms, Size={sq['size_bytes']/1024:.1f}KB, 1-Hop Callers={sq['callers_ms']:.3f}ms/query, 4-Hop Impact={sq['impact_ms']:.3f}ms/query")
    print(f"Kùzu Graph: Write={kz['write_ms']:.1f}ms, Size={kz['size_bytes']/1024:.1f}KB, 1-Hop Callers={kz['callers_ms']:.3f}ms/query, 4-Hop Impact={kz['impact_ms']:.3f}ms/query")
