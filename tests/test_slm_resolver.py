"""Unit and integration tests for Chronos SLM Zero-Path Resolver."""

from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.context.resolver import DeterministicContextResolver
from chronos.context.slm_resolver import LocalSLMResolver, SLMResolutionResult
from chronos.core.db import get_connection


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).parent.parent.resolve()


def test_slm_resolver_with_natural_words_prompt(repo_root: Path):
    """Test that an SLM prompt in natural words correctly specifies exact repository files."""
    resolver = LocalSLMResolver(repo_root=repo_root)

    # Prompt with NO explicit file paths
    prompt = "Prevent race condition in lock ledger when lock release coincides with lock heartbeat renewal"
    result = resolver.resolve_prompt(prompt)

    assert isinstance(result, SLMResolutionResult)
    assert len(result.suspected_files) > 0
    # Must specify coordination ledger
    assert any("ledger.py" in f for f in result.suspected_files)
    assert (repo_root / result.suspected_files[0]).exists()


def test_slm_resolver_graph_recursion_natural_words(repo_root: Path):
    """Test resolving circular reference recursion in graph traversal with natural words."""
    resolver = LocalSLMResolver(repo_root=repo_root)

    prompt = "Fix circular reference recursion edge case so that mutually recursive functions do not cause infinite recursion during traversal."
    result = resolver.resolve_prompt(prompt)

    assert isinstance(result, SLMResolutionResult)
    assert any("graph.py" in f for f in result.suspected_files)


def test_slm_resolver_resilient_fallback_on_failure(repo_root: Path):
    """Test that SLM resolver gracefully falls back to empty result on error without crashing."""
    resolver = LocalSLMResolver(repo_root=repo_root, model="non_existent_model_xyz", timeout_seconds=1)

    # Force error by mocking subprocess.run
    with patch("subprocess.run", side_effect=RuntimeError("SLM unreachable")):
        result = resolver.resolve_prompt("Some arbitrary task description")
        assert isinstance(result, SLMResolutionResult)
        assert result.suspected_files == []


def test_zero_path_context_resolver_with_slm_integration(repo_root: Path):
    """Test full DeterministicContextResolver combining SLM inference, AST graph, and test discovery."""
    con = get_connection(":memory:")
    nodes, edges = index_repository(repo_root)
    store = SQLiteGraphStore(con)
    store.ingest(nodes, edges)

    # Mock SLM to return exact file for deterministic test speed
    mock_slm = MagicMock()
    mock_slm.resolve_prompt.return_value = SLMResolutionResult(
        suspected_files=["chronos/coordination/ledger.py"],
        suspected_symbols=["LockLedger", "release_lock"],
        domain_concepts=["concurrency", "locking"],
        intent="bug_fix",
        rationale="Lock concurrency issue in coordination ledger",
    )

    resolver = DeterministicContextResolver(
        repo_root=repo_root,
        graph_store=store,
        slm_resolver=mock_slm,
    )

    prompt = "Prevent race condition during lock renewal"
    res = resolver.resolve(prompt=prompt)

    # Check that target_files contains chronos/coordination/ledger.py
    assert any("ledger.py" in f for f in res.target_files)
    # Check that associated test file was discovered
    assert any("test_coordination.py" in tf for tf in res.test_files)
    # Check that provenance summary mentions SLM inference
    assert "SLM Semantic Inference" in res.provenance_summary
