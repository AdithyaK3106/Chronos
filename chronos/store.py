"""Graph store setup.

Kuzu is embedded (a file, not a server), which is what lets P0-5's "one install
step, no external services" actually hold. Upstream marks it deprecated, so it is
isolated here: swapping to FalkorDB/Neo4j means changing open_driver() only, and
CHRONOS_DB_URI already routes there.
"""

import os
from pathlib import Path


def db_path() -> Path:
    p = Path(os.environ.get("CHRONOS_DB", Path.home() / ".chronos" / "graph.kz"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def open_driver():
    uri = os.environ.get("CHRONOS_DB_URI")
    if uri:  # e.g. bolt://... for Neo4j, when a partner outgrows the embedded store
        from graphiti_core.driver.neo4j_driver import Neo4jDriver
        return Neo4jDriver(uri, os.environ.get("CHRONOS_DB_USER", "neo4j"),
                           os.environ.get("CHRONOS_DB_PASSWORD", "password"))
    import warnings
    with warnings.catch_warnings():  # kuzu deprecation notice is expected and handled
        warnings.simplefilter("ignore")
        from graphiti_core.driver.kuzu_driver import KuzuDriver
        return KuzuDriver(db=str(db_path()))


async def ensure_schema(driver):
    try:
        await driver.build_indices_and_constraints()
    except Exception:
        pass  # already built; kuzu raises on re-create
