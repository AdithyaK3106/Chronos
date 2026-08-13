"""Sync layer: upstream structural graph -> Graphiti bi-temporal facts.

The core v1 deliverable (P0-2). Design rules that fall out of the PRD:

* Upstream owns current structure; Graphiti owns history. We never write to
  upstream, and we hold no state of our own -- node/edge UUIDs are derived
  deterministically (uuid5) from (group_id, identity), so the whole store is
  re-derivable from upstream + Graphiti alone if this process dies mid-run.
* We do NOT use Graphiti's add_episode/add_triplet. Both run LLM entity
  extraction + embedding-based dedup on every fact. AST facts are already
  structured -- there is nothing to extract -- and LLM dedup would fuzzily merge
  distinct same-named functions. We write EntityNode/EntityEdge .save() directly,
  which is the documented model layer and sets valid_at/invalid_at natively.
  ponytail: this is why Chronos needs no LLM key and no network egress.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from graphiti_core.driver.driver import GraphProvider
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk

NS = uuid.UUID("7f3d2c1a-0b6e-4d4a-9c2f-5e1a8b7c6d55")  # chronos namespace

# Graphiti requires an embedding vector on every node/edge. We never do semantic
# search over these facts -- lookups are exact, by structural identity -- so the
# vector is dead weight, and its size dominates write cost: serializing 1024 floats
# per row across the python/kuzu boundary measured 17.3s per 1000 rows vs 0.35s at
# dim=4 (50x). A length-1 vector keeps the column well-typed at minimum cost.
# ponytail: if semantic search over code facts is ever wanted, this becomes a real
# embedder and writes get proportionally slower -- that is the trade, priced here.
DIM = 1
_ZERO = [0.0] * DIM


class _NullEmbedder:
    """Satisfies the embedder parameter without existing as a real client.

    add_nodes_and_edges_bulk only touches it when an embedding is None; we always
    pre-set _ZERO, so these methods should never run. They raise rather than
    silently returning a vector, so a future code path that *does* need embeddings
    fails loudly instead of writing junk.
    """

    async def create(self, *a, **k):
        raise RuntimeError("chronos does not embed: structural facts are looked up exactly")

    async def create_batch(self, *a, **k):
        raise RuntimeError("chronos does not embed: structural facts are looked up exactly")


# Batched writes against graphiti's kuzu schema. Graphiti's own bulk helper issues
# one statement per row through its driver abstraction (~30 writes/s measured);
# the same rows via UNWIND run ~1000x faster, which is the difference between a
# multi-hour and a sub-minute initial index on a real repo. Rows are written in
# exactly the shape graphiti's readers expect -- verified by reading every write
# back through EntityEdge.get_by_uuid in the test suite.
# ponytail: falls back to graphiti's helper on any non-kuzu driver, so a partner
# on neo4j still works, just slower.
_UNWIND_NODES = """
UNWIND $rows AS r
MERGE (n:Entity {uuid: r.uuid})
SET n.name = r.name, n.group_id = r.group_id, n.labels = r.labels,
    n.created_at = r.created_at, n.name_embedding = r.name_embedding,
    n.summary = r.summary, n.attributes = r.attributes
"""

_UNWIND_EDGES = """
UNWIND $rows AS r
MATCH (src:Entity {uuid: r.source_node_uuid}), (dst:Entity {uuid: r.target_node_uuid})
MERGE (src)-[:RELATES_TO]->(e:RelatesToNode_ {uuid: r.uuid})-[:RELATES_TO]->(dst)
SET e.group_id = r.group_id, e.created_at = r.created_at, e.name = r.name,
    e.fact = r.fact, e.fact_embedding = r.fact_embedding, e.episodes = r.episodes,
    e.valid_at = r.valid_at, e.attributes = r.attributes,
    // nullable timestamps: an all-NULL batch would otherwise be inferred as STRING
    e.expired_at = CAST(r.expired_at AS TIMESTAMP),
    e.invalid_at = CAST(r.invalid_at AS TIMESTAMP)
"""

CHUNK = 1000


async def _bulk(driver, nodes: list, edges: list):
    if getattr(driver, "provider", None) != GraphProvider.KUZU:
        await add_nodes_and_edges_bulk(driver, [], [], nodes, edges, _NullEmbedder())
        return

    for i in range(0, len(nodes), CHUNK):
        rows = [{
            "uuid": n.uuid, "name": n.name, "group_id": n.group_id,
            "labels": list(set(n.labels + ["Entity"])), "created_at": n.created_at,
            "name_embedding": n.name_embedding, "summary": n.summary or "",
            "attributes": json.dumps(n.attributes or {}),
        } for n in nodes[i:i + CHUNK]]
        await driver.execute_query(_UNWIND_NODES, rows=rows)

    for i in range(0, len(edges), CHUNK):
        rows = [{
            "uuid": e.uuid, "source_node_uuid": e.source_node_uuid,
            "target_node_uuid": e.target_node_uuid, "group_id": e.group_id,
            "created_at": e.created_at, "name": e.name, "fact": e.fact,
            "fact_embedding": e.fact_embedding, "episodes": e.episodes or [],
            "expired_at": e.expired_at, "valid_at": e.valid_at,
            "invalid_at": e.invalid_at, "attributes": json.dumps(e.attributes or {}),
        } for e in edges[i:i + CHUNK]]
        await driver.execute_query(_UNWIND_EDGES, rows=rows)


def _uuid(group_id: str, kind: str, ident: str) -> str:
    return str(uuid.uuid5(NS, f"{group_id}|{kind}|{ident}"))


def node_identity(n: dict) -> str:
    """Stable identity for a symbol.

    Prefers upstream's qualified_name, which it declares UNIQUE per project and
    which disambiguates nested closures (two `getAll`s in one file) and same-named
    folder/file nodes. Falls back to path+name+kind for inputs that have no
    qualified name.

    Deliberately excludes line numbers either way -- a function shifting down 10
    lines is not a new function, and treating it as one would churn history on
    every edit.
    """
    qn = n.get("qname")
    if qn:
        return f"{qn}::{n.get('kind','Symbol')}"
    return f"{n.get('path','')}::{n.get('name','')}::{n.get('kind','Symbol')}"


def edge_identity(src: str, etype: str, dst: str) -> str:
    return f"{src}-[{etype}]->{dst}"


@dataclass
class SyncStats:
    nodes: int = 0
    edges_added: int = 0
    edges_invalidated: int = 0
    edges_unchanged: int = 0

    def __str__(self):
        return (f"nodes={self.nodes} added={self.edges_added} "
                f"invalidated={self.edges_invalidated} unchanged={self.edges_unchanged}")


class Syncer:
    def __init__(self, driver, group_id: str):
        self.driver = driver
        self.group_id = group_id

    async def _live_edges(self) -> dict[str, dict]:
        """Currently-valid edges in Graphiti, keyed by our deterministic uuid."""
        q = """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id = $g AND e.invalid_at IS NULL
        RETURN e.uuid AS uuid, e.name AS name
        """
        recs, _, _ = await self.driver.execute_query(q, g=self.group_id)
        return {r["uuid"]: dict(r) for r in recs}

    async def sync(self, nodes: dict[str, dict], edges: list[tuple[str, str, str]],
                   at: datetime | None = None) -> SyncStats:
        """Reconcile upstream's current structure into Graphiti at time `at`.

        Bi-temporal semantics:
          valid_at   = when this fact became true in the codebase (commit time)
          invalid_at = when it stopped being true (set on supersession)
        """
        at = at or datetime.now(timezone.utc)
        st = SyncStats()

        # 1. Build nodes. Same identity -> same uuid -> idempotent overwrite.
        node_uuid: dict[str, str] = {}
        pending_nodes: list[EntityNode] = []
        for raw_id, n in nodes.items():
            u = _uuid(self.group_id, "node", node_identity(n))
            node_uuid[raw_id] = u
            pending_nodes.append(EntityNode(
                uuid=u, name=n["name"], group_id=self.group_id,
                labels=["Entity", n.get("kind") or "Symbol"],
                created_at=at, summary=n.get("path", ""),
                # attributes is an explicit list, so a new node field must be
                # added here or it is silently dropped on the way to the graph.
                attributes={"path": n.get("path", ""), "kind": n.get("kind", "Symbol"),
                            "language": n.get("language", "unknown")},
                name_embedding=_ZERO,
            ))
            st.nodes += 1

        # 2. Desired edge set from upstream (skip edges whose endpoints we dropped).
        desired: dict[str, tuple[str, str, str]] = {}
        for s, t, d in edges:
            if s not in node_uuid or d not in node_uuid:
                continue
            ident = edge_identity(node_identity(nodes[s]), t, node_identity(nodes[d]))
            desired[_uuid(self.group_id, "edge", ident)] = (node_uuid[s], t, node_uuid[d])

        live = await self._live_edges()

        # 3. Add facts that are new.
        name_by_uuid = {u: nodes[r]["name"] for r, u in node_uuid.items()}
        pending_edges: list[EntityEdge] = []
        for u, (su, etype, du) in desired.items():
            if u in live:
                st.edges_unchanged += 1
                continue
            pending_edges.append(EntityEdge(
                uuid=u, source_node_uuid=su, target_node_uuid=du, name=etype,
                fact=f"{name_by_uuid.get(su,'?')} {etype} {name_by_uuid.get(du,'?')}",
                group_id=self.group_id, created_at=at, valid_at=at,
                fact_embedding=_ZERO,
            ))
            st.edges_added += 1

        # One transaction for the whole batch. Per-object .save() costs a round
        # trip each (~30 writes/s measured on kuzu), which blows the sync SLA on a
        # real repo; this is the same public API graphiti uses for bulk ingest.
        # Embeddings are pre-set to zero vectors above, so no embedder/LLM is
        # ever invoked despite the required parameter.
        if pending_nodes or pending_edges:
            await _bulk(self.driver, pending_nodes, pending_edges)

        # 4. Supersede facts that upstream no longer has: close them at `at`
        #    rather than deleting, so "as of" queries before now still see them.
        gone = list(live.keys() - desired.keys())
        if gone:
            await self.driver.execute_query(
                """MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
                   WHERE list_contains($us, e.uuid)
                   SET e.invalid_at = $t, e.expired_at = $t""",
                us=gone, t=at,
            )
            st.edges_invalidated = len(gone)
            # Cross-wedge trigger 2 (observation only -- runs after the write is
            # committed and cannot alter it). Warns when a node just became
            # deprecated with no Wedge 4 rule covering it, which is the silent
            # gap where agents keep using a superseded symbol and CI passes.
            await self._coverage_check(gone, at)

        return st

    async def _coverage_check(self, gone_uuids, at):
        try:
            recs, _, _ = await self.driver.execute_query(
                """MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
                   WHERE list_contains($us, e.uuid)
                   RETURN DISTINCT m.name AS name""", us=gone_uuids)
            from . import triggers
            seen = set()
            for r in recs:
                name = dict(r).get("name")
                if name and name not in seen:
                    seen.add(name)
                    triggers.on_deprecation(name, valid_at=at)
        except Exception:  # noqa: BLE001 -- best-effort by contract
            pass


def content_hash(nodes: dict, edges: list) -> str:
    """Cheap change detector so an unchanged repo costs one hash, not a full sync."""
    h = hashlib.sha256()
    for k in sorted(nodes):
        h.update(node_identity(nodes[k]).encode())
    for e in sorted(edges):
        h.update("|".join(e).encode())
    return h.hexdigest()[:16]
