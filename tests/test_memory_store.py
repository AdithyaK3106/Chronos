"""Unit tests for Chronos Epistemic Living Memory Store."""

import sqlite3
import pytest
from pathlib import Path

from chronos.core.db import get_connection
from chronos.memory.models import (
    MemoryRecord,
    MemoryTier,
    OutcomeType,
    VerificationStatus,
)
from chronos.memory.store import ChronosMemoryStore


@pytest.fixture
def mem_store(tmp_path: Path):
    db_file = tmp_path / "test_memory.db"
    con = get_connection(db_file)
    store = ChronosMemoryStore(con=con)
    yield store
    con.close()


def test_remember_and_recall_basic(mem_store: ChronosMemoryStore):
    mid = mem_store.remember(
        content="Cycle prevention in SQLiteGraphStore.as_of_impact requires tracking visited paths in recursive CTE.",
        repo_namespace="chronos",
        tier=MemoryTier.LEARNED,
        summary="Cycle prevention in graph traversal",
        verification_status=VerificationStatus.VERIFIED,
        importance=0.9,
        tags=["graph", "cycle_detection", "sqlite"],
    )
    assert mid > 0

    # Recall by keywords
    results = mem_store.recall("cycle prevention SQLiteGraphStore", repo_namespace="chronos")
    assert len(results) > 0
    top = results[0]
    assert top.id == mid
    assert "Cycle prevention" in top.content
    assert top.tier == MemoryTier.LEARNED
    assert top.verification_status == VerificationStatus.VERIFIED
    assert top.score > 0.0


def test_namespace_isolation(mem_store: ChronosMemoryStore):
    # Remember memory in repo A
    mem_store.remember(
        content="Database credentials should be loaded from vault in auth service.",
        repo_namespace="repo_auth",
        tier=MemoryTier.CORE,
    )
    # Remember memory in repo B
    mem_store.remember(
        content="Use gRPC streaming for telemetry event pipelines.",
        repo_namespace="repo_telemetry",
        tier=MemoryTier.PROCEDURAL,
    )
    # Remember global memory
    mem_store.remember(
        content="Never commit private keys to version control.",
        repo_namespace="__global__",
        tier=MemoryTier.CORE,
    )

    # Search in repo_auth
    results_auth = mem_store.recall("vault credentials", repo_namespace="repo_auth")
    assert any("vault" in r.content for r in results_auth)
    # Ensure repo_telemetry memory is NOT returned in repo_auth
    assert not any("gRPC streaming" in r.content for r in results_auth)

    # Search global memory in repo_auth
    results_global = mem_store.recall("private keys", repo_namespace="repo_auth")
    assert any("private keys" in r.content for r in results_global)


def test_update_memory_lineage_and_supersedes(mem_store: ChronosMemoryStore):
    # Store initial fact
    v1_id = mem_store.remember(
        content="Default token budget for context assembly is 2000 tokens.",
        repo_namespace="chronos",
        tier=MemoryTier.LEARNED,
    )

    # Update fact with new value
    v2_id = mem_store.update_memory(
        old_id=v1_id,
        new_content="Default token budget for context assembly was increased to 4000 tokens.",
        summary="Token budget increased to 4000",
    )
    assert v2_id != v1_id

    # Verify v1 is archived
    v1_row = mem_store.con.execute("SELECT archived FROM memories WHERE id = ?", (v1_id,)).fetchone()
    assert v1_row["archived"] == 1

    # Verify v2 points to v1 as supersedes
    v2_row = mem_store.con.execute("SELECT supersedes, archived FROM memories WHERE id = ?", (v2_id,)).fetchone()
    assert v2_row["supersedes"] == v1_id
    assert v2_row["archived"] == 0

    # Recall with current_only=True (default) should only return v2
    results = mem_store.recall("token budget", repo_namespace="chronos", current_only=True)
    assert len(results) == 1
    assert results[0].id == v2_id
    assert "4000 tokens" in results[0].content


def test_fts5_index_consistency_on_delete_and_archive(mem_store: ChronosMemoryStore):
    mid = mem_store.remember(
        content="Temporary scratchpad note that should be deleted.",
        repo_namespace="chronos",
        tier=MemoryTier.WORKING,
    )
    assert len(mem_store.recall("scratchpad", repo_namespace="chronos")) == 1

    # Archive memory
    mem_store.archive_memory(mid)
    # Search should exclude archived memories by default
    assert len(mem_store.recall("scratchpad", repo_namespace="chronos")) == 0

    # Delete memory
    mem_store.delete_memory(mid)
    # Verify FTS index trigger deleted row
    fts_rows = mem_store.con.execute("SELECT * FROM memories_fts WHERE id = ?", (mid,)).fetchall()
    assert len(fts_rows) == 0


def test_secret_scrubbing_on_remember(mem_store: ChronosMemoryStore):
    # Sensitive credentials inside text
    raw_text = "Connected using sk-abcdef12345678901234567890 and password='supersecret123'."
    mid = mem_store.remember(
        content=raw_text,
        repo_namespace="chronos",
        evidence="Authorization key chron_12345678901234567890123456 attached",
    )
    row = mem_store.con.execute("SELECT content, evidence FROM memories WHERE id = ?", (mid,)).fetchone()
    assert "sk-[REDACTED]" in row["content"]
    assert "sk-abcdef1234567890" not in row["content"]
    assert "supersecret123" not in row["content"]
    assert "chron_[REDACTED]" in row["evidence"]


def test_procedural_rules_and_working_state(mem_store: ChronosMemoryStore):
    # Add procedural rule
    mem_store.add_procedure("Always run pytest before committing code changes.", repo_namespace="chronos")
    mem_store.add_procedure("Never bypass security gates without administrative approval.", repo_namespace="chronos")

    rules = mem_store.get_procedures(repo_namespace="chronos")
    assert len(rules) == 2
    assert any("pytest" in r for r in rules)
    assert any("security gates" in r for r in rules)

    # Save working state
    mem_store.save_state("Step 2/4 completed: AST indexer updated.", repo_namespace="chronos")
    state = mem_store.get_state(repo_namespace="chronos")
    assert len(state) == 1
    assert "Step 2/4" in state[0].content


def test_entity_indexing(mem_store: ChronosMemoryStore):
    mem_store.remember(
        content="Refactored SQLiteGraphStore.as_of_callers in chronos/context/graph.py to handle qualified IDs.",
        repo_namespace="chronos",
    )
    # Verify entities table has extracted entities
    entities = mem_store.con.execute("SELECT name, type FROM entities").fetchall()
    names = {r["name"] for r in entities}
    assert "chronos/context/graph.py" in names
    assert any("as_of_callers" in n or "SQLiteGraphStore" in n for n in names)


def test_stats_aggregation(mem_store: ChronosMemoryStore):
    mem_store.remember("Rule 1", repo_namespace="chronos", tier=MemoryTier.CORE)
    mem_store.remember("Rule 2", repo_namespace="chronos", tier=MemoryTier.LEARNED)
    mid_failed = mem_store.remember(
        "Hypothesis 1",
        repo_namespace="chronos",
        tier=MemoryTier.LEARNED,
        outcome=OutcomeType.FAILED_HYPOTHESIS,
    )
    mem_store.archive_memory(mid_failed)

    stats = mem_store.stats(repo_namespace="chronos")
    assert stats.total_memories == 3
    assert stats.archived_count == 1
    assert stats.by_tier.get("core") == 1
    assert stats.by_tier.get("learned") == 2
    assert stats.by_outcome.get("failed_hypothesis") == 1
