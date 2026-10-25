"""Unit tests for Chronos Deterministic Zero-Path Context Resolver (MVP)."""

import sqlite3
from pathlib import Path
import pytest

from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.context.resolver import (
    DeterministicContextResolver,
    HarvestedFeatures,
    ResolutionResult,
    STOP_WORDS,
)
from chronos.core.db import get_connection

REPO_ROOT = Path(__file__).parent.parent.resolve()


@pytest.fixture(scope="module")
def indexed_chronos_graph():
    """Index the actual chronos repository once for high-fidelity evaluation."""
    con = get_connection(":memory:")
    graph_store = SQLiteGraphStore(con)
    nodes, edges = index_repository(REPO_ROOT / "chronos")
    graph_store.ingest(nodes, edges)
    return con, graph_store


def test_harvest_features():
    prompt = """
    Please fix the issue in File "src/pipeline.py", line 42 where DatabaseConnectionError occurs.
    The method SQLiteGraphStore.as_of_impact has an infinite loop when resolving cyclic_dependency.
    Do not touch chronos/security/sanitizer.py or modify active permissions.
    """
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=None)  # type: ignore
    features: HarvestedFeatures = resolver.harvest_features(prompt)

    # Check explicit paths and stack trace files
    assert any("pipeline.py" in p for p in features.stack_trace_files)
    assert any("sanitizer.py" in p for p in features.explicit_paths)

    # Check symbols
    assert "DatabaseConnectionError" in features.exceptions or "DatabaseConnectionError" in features.camel_case_symbols
    assert any("as_of_impact" in s for s in features.snake_case_symbols + features.dotted_symbols)
    assert "cyclic_dependency" in features.snake_case_symbols

    # Check stop words are filtered from keywords
    for word in ["please", "the", "in", "has", "an", "when", "or"]:
        assert word not in features.keywords


def test_zero_path_resolution_exact_ground_truth(indexed_chronos_graph):
    _, graph_store = indexed_chronos_graph
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=graph_store)

    prompt = "Fix the infinite loop when traversing graph nodes."
    res: ResolutionResult = resolver.resolve(prompt=prompt)

    # 1. Target files should resolve to chronos/context/graph.py
    assert any("chronos/context/graph.py" in tf for tf in res.target_files), f"Expected graph.py, got {res.target_files}"

    # 2. Top candidate score should be substantial
    assert len(res.candidates) > 0
    assert res.candidates[0].score >= 15.0

    # 3. Associated test files should identify test_context_graph.py
    assert any("test_context_graph.py" in tf for tf in res.test_files), f"Expected test_context_graph.py, got {res.test_files}"

    # 4. Provenance rationale is clearly formatted
    assert "Retrieval Provenance & Relevance Rationale" in res.provenance_summary


def test_eval_ground_truth_suite(indexed_chronos_graph):
    """Evaluate against the ground-truth benchmark tasks from the implementation plan."""
    _, graph_store = indexed_chronos_graph
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=graph_store)

    test_cases = [
        # (Task Prompt, Expected Ground Truth File, Expected Ground Truth Symbol)
        (
            "Prevent race condition in lock ledger when lock release coincides with renewal",
            "chronos/coordination/ledger.py",
            "acquire_lock",
        ),
        (
            "Implement regex-based symbol pattern filtering in query_symbols",
            "chronos/context/graph.py",
            "query_symbols_lexical",
        ),
        (
            "Add JSON Schema export utility for active security rules",
            "chronos/verification/rule_store.py",
            "get_active_blocking_rules",
        ),
        (
            "Decouple direct SQLite connection handling in audit logger into context manager",
            "chronos/audit/logger.py",
            "append_audit_event",
        ),
        (
            "Optimize SQLiteGraphStore.as_of_callees query performance",
            "chronos/context/graph.py",
            "as_of_callees",
        ),
        (
            "Refactor error formatting in chronos/security/sanitizer.py",
            "chronos/security/sanitizer.py",
            "validate_path_access",
        ),
    ]

    top3_hits = 0
    mrr_sum = 0.0

    for prompt, expected_file, expected_sym in test_cases:
        res = resolver.resolve(prompt)
        matched_file_ranks = [i + 1 for i, tf in enumerate(res.target_files) if expected_file in tf]

        if matched_file_ranks:
            rank = matched_file_ranks[0]
            if rank <= 3:
                top3_hits += 1
            mrr_sum += 1.0 / rank
        else:
            # Check if any candidate has the expected file
            cand_ranks = [i + 1 for i, c in enumerate(res.candidates) if expected_file in c.path]
            if cand_ranks:
                rank = cand_ranks[0]
                if rank <= 3:
                    top3_hits += 1
                mrr_sum += 1.0 / rank

    recall_at_3 = top3_hits / len(test_cases)
    mrr = mrr_sum / len(test_cases)

    # Acceptance threshold: Recall@3 >= 80% on core Chronos tasks
    assert recall_at_3 >= 0.80, f"Recall@3 was {recall_at_3:.2f}, expected >= 0.80"
    assert mrr >= 0.70, f"MRR was {mrr:.2f}, expected >= 0.70"


def test_ambiguous_query_returns_multiple_candidates(indexed_chronos_graph):
    _, graph_store = indexed_chronos_graph
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=graph_store)

    prompt = "Update database connection error logging and schema"
    res = resolver.resolve(prompt)

    # Should match candidates across db.py or logger.py
    assert len(res.candidates) >= 1
    found_db = any("db.py" in c.path for c in res.candidates)
    found_log = any("logger.py" in c.path for c in res.candidates)
    assert found_db or found_log


def test_no_match_query_graceful_handling(indexed_chronos_graph):
    _, graph_store = indexed_chronos_graph
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=graph_store)

    prompt = "How do I bake sourdough bread with whole wheat flour and chocolate sprinkles?"
    res = resolver.resolve(prompt)

    assert len(res.candidates) == 0
    assert len(res.target_files) == 0
    assert "No high-confidence symbols matched" in res.provenance_summary


def test_empty_or_stale_index_handling():
    """Verify resolver operates cleanly without exceptions when database has no nodes."""
    con = get_connection(":memory:")
    graph_store = SQLiteGraphStore(con)
    resolver = DeterministicContextResolver(repo_root=REPO_ROOT, graph_store=graph_store)

    res = resolver.resolve("Fix the graph node traversal loop")
    assert res.target_files == []
    assert res.candidates == []
    assert "No high-confidence symbols matched" in res.provenance_summary
