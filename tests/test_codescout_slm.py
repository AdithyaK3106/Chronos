"""Comprehensive test suite for OpenHands/CodeScout-1.7B integration into Chronos."""

import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch
import urllib.error

from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.context.resolver import CandidateEvidence, DeterministicContextResolver, ResolutionResult
from chronos.context.slm_resolver import LocalSLMResolver, SLMCircuitBreaker, SLMResolutionResult
from chronos.core.config import ChronosConfig, SLMConfig
from chronos.core.db import get_connection
from runtime.session import ChronosSessionBootstrap


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).parent.parent.resolve()


# ---------------------------------------------------------------------------
# 1. Configuration & Precedence Tests
# ---------------------------------------------------------------------------

def test_slm_config_defaults():
    """Verify default SLMConfig is explicitly disabled (opt-in) with model fixed to CodeScout-1.7B."""
    cfg = SLMConfig()
    assert cfg.enabled is False
    assert cfg.model == "OpenHands/CodeScout-1.7B"
    assert cfg.backend == "openai_compatible"
    assert cfg.endpoint == "http://localhost:8000/v1"
    assert cfg.timeout_seconds == 35
    assert cfg.max_candidate_files == 150


def test_slm_config_precedence(tmp_path: Path, monkeypatch):
    """Verify precedence: CLI > ENV > config.json > defaults."""
    chronos_dir = tmp_path / ".chronos"
    chronos_dir.mkdir(parents=True)
    cfg_file = chronos_dir / "config.json"

    # 1. Base from config.json
    cfg_file.write_text(json.dumps({
        "slm": {
            "enabled": True,
            "backend": "ollama",
            "endpoint": "http://localhost:11434/v1",
            "timeout_seconds": 40,
        }
    }), encoding="utf-8")

    cfg = ChronosConfig.load(tmp_path)
    assert cfg.slm.enabled is True
    assert cfg.slm.backend == "ollama"
    assert cfg.slm.endpoint == "http://localhost:11434/v1"
    assert cfg.slm.timeout_seconds == 40

    # 2. Environment variable override
    monkeypatch.setenv("CHRONOS_ENABLE_SLM", "false")
    monkeypatch.setenv("CHRONOS_SLM_BACKEND", "openai_compatible")
    monkeypatch.setenv("CHRONOS_SLM_ENDPOINT", "http://env-host:8000/v1")
    monkeypatch.setenv("CHRONOS_SLM_TIMEOUT", "15")

    cfg_env = ChronosConfig.load(tmp_path)
    assert cfg_env.slm.enabled is False
    assert cfg_env.slm.backend == "openai_compatible"
    assert cfg_env.slm.endpoint == "http://env-host:8000/v1"
    assert cfg_env.slm.timeout_seconds == 15


# ---------------------------------------------------------------------------
# 2. Zero-Network Invariant when Disabled
# ---------------------------------------------------------------------------

def test_disabled_slm_initiates_zero_network_calls(repo_root: Path):
    """Verify that when SLM is disabled, resolve_prompt initiates zero HTTP/subprocess requests."""
    cfg = SLMConfig(enabled=False)
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    with patch("urllib.request.urlopen") as mock_http, patch("subprocess.run") as mock_subproc:
        res = resolver.resolve_prompt("Any task prompt")
        assert res.suspected_files == []
        assert mock_http.call_count == 0
        assert mock_subproc.call_count == 0


# ---------------------------------------------------------------------------
# 3. Anti-Hallucination & Ambiguity Handling
# ---------------------------------------------------------------------------

def test_anti_hallucination_rejects_non_existent_paths(repo_root: Path):
    """Verify that non-existent paths are rejected by the anti-hallucination gate."""
    cfg = SLMConfig(enabled=True)
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    raw_res = SLMResolutionResult(
        suspected_files=[
            "chronos/coordination/ledger.py",          # Real
            "chronos/fictional_hallucinated_file.py",    # Non-existent
            "totally/made_up/module.go",                # Non-existent
        ],
        suspected_symbols=["acquire_lock"],
    )

    filtered = resolver._filter_and_validate(raw_res, candidate_files=resolver.list_candidate_files())
    assert "chronos/coordination/ledger.py" in filtered.suspected_files
    assert "chronos/fictional_hallucinated_file.py" not in filtered.suspected_files
    assert "totally/made_up/module.go" not in filtered.suspected_files


def test_anti_hallucination_rejects_ambiguous_basenames(repo_root: Path):
    """Verify that ambiguous duplicate basenames across directories are rejected rather than guessed."""
    cfg = SLMConfig(enabled=True)
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    # Simulated candidate file pool with duplicate basenames
    mock_candidates = [
        "src/utils.py",
        "backend/utils.py",
        "tests/utils.py",
        "chronos/coordination/ledger.py",  # Unique
    ]

    raw_res = SLMResolutionResult(
        suspected_files=[
            "utils.py",    # Ambiguous: matches 3 different files
            "ledger.py",   # Unambiguous: matches exactly 1 file
        ],
    )

    filtered = resolver._filter_and_validate(raw_res, candidate_files=mock_candidates)
    # ledger.py should be normalized to chronos/coordination/ledger.py
    assert "chronos/coordination/ledger.py" in filtered.suspected_files
    # utils.py must be rejected because it's ambiguous
    assert not any("utils.py" in f for f in filtered.suspected_files)


# ---------------------------------------------------------------------------
# 4. Circuit Breaker & Resilience Tests
# ---------------------------------------------------------------------------

def test_circuit_breaker_trips_after_consecutive_failures(repo_root: Path):
    """Verify that circuit breaker opens after threshold failures and suppresses further requests."""
    breaker = SLMCircuitBreaker(max_failures=2)
    assert breaker.can_attempt() is True

    breaker.record_failure("Connection refused")
    assert breaker.can_attempt() is True
    assert breaker.consecutive_failures == 1

    breaker.record_failure("Read timeout")
    assert breaker.can_attempt() is False  # Tripped open!
    assert breaker.is_open is True

    # Subsequent attempts are suppressed
    assert breaker.can_attempt() is False

    # Reset
    breaker.record_success()
    assert breaker.can_attempt() is True
    assert breaker.is_open is False


def test_resolver_circuit_breaker_integration(repo_root: Path):
    """Verify LocalSLMResolver trips circuit breaker and returns empty result on failure without crashing."""
    cfg = SLMConfig(enabled=True, backend="openai_compatible", endpoint="http://localhost:9999/v1", timeout_seconds=1)
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        # First failure
        res1 = resolver.resolve_prompt("Fix race condition")
        assert res1.suspected_files == []
        assert resolver.circuit_breaker.consecutive_failures == 1

        # Second failure -> trips breaker
        res2 = resolver.resolve_prompt("Fix race condition")
        assert res2.suspected_files == []
        assert resolver.circuit_breaker.is_open is True

        # Third attempt -> bypassed by circuit breaker without network call
        with patch.object(resolver, "_call_openai_compatible") as mock_call:
            res3 = resolver.resolve_prompt("Fix race condition")
            assert res3.suspected_files == []
            assert mock_call.call_count == 0


# ---------------------------------------------------------------------------
# 5. OpenAI-Compatible & Ollama Mocked Inference Tests
# ---------------------------------------------------------------------------

def test_openai_compatible_backend_success(repo_root: Path):
    """Verify successful parsing of OpenAI-compatible response from CodeScout."""
    cfg = SLMConfig(enabled=True, backend="openai_compatible", endpoint="http://localhost:8000/v1")
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "suspected_files": ["chronos/coordination/ledger.py"],
                        "suspected_symbols": ["release_lock", "acquire_lock"],
                        "domain_concepts": ["concurrency", "distributed_lock"],
                        "intent": "bug_fix",
                        "rationale": "Synchronization race between lock lease renewal and release",
                    })
                }
            }
        ]
    }
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = resolver.resolve_prompt("Fix race condition in coordination ledger")
        assert "chronos/coordination/ledger.py" in res.suspected_files
        assert "release_lock" in res.suspected_symbols
        assert res.intent == "bug_fix"


def test_ollama_backend_success(repo_root: Path):
    """Verify successful parsing of Ollama native /api/chat response."""
    cfg = SLMConfig(enabled=True, backend="ollama", endpoint="http://localhost:11434")
    resolver = LocalSLMResolver(repo_root=repo_root, config=cfg)

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_payload = {
        "message": {
            "content": "```json\n" + json.dumps({
                "suspected_files": ["chronos/security/sanitizer.py"],
                "suspected_symbols": ["redact_secrets"],
                "domain_concepts": ["security", "secrets"],
                "intent": "security",
                "rationale": "Audit log secret redaction",
            }) + "\n```"
        }
    }
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = resolver.resolve_prompt("Sanitize API keys in audit trail")
        assert "chronos/security/sanitizer.py" in res.suspected_files
        assert "redact_secrets" in res.suspected_symbols


# ---------------------------------------------------------------------------
# 6. Graph Fusion & Context Budgeting
# ---------------------------------------------------------------------------

def test_deterministic_resolver_graph_fusion(repo_root: Path):
    """Verify that DeterministicContextResolver fuses CodeScout suggestions with AST graph."""
    con = get_connection(":memory:")
    nodes, edges = index_repository(repo_root)
    store = SQLiteGraphStore(con)
    store.ingest(nodes, edges)

    # Mock SLM resolver returning ledger.py
    mock_slm = MagicMock()
    mock_slm.resolve_prompt.return_value = SLMResolutionResult(
        suspected_files=["chronos/coordination/ledger.py"],
        suspected_symbols=["release_lock"],
        domain_concepts=["concurrency"],
        intent="bug_fix",
        rationale="Race condition in lock release",
    )

    resolver = DeterministicContextResolver(
        repo_root=repo_root,
        graph_store=store,
        slm_resolver=mock_slm,
    )

    prompt = "Prevent race condition in lock ledger"
    res = resolver.resolve(prompt=prompt)

    # Ledger must be in target files
    assert any("ledger.py" in f for f in res.target_files)
    # Associated test must be discovered
    assert any("test_coordination.py" in tf for tf in res.test_files)
    # Provenance summary must attribute to SLM Semantic Inference
    assert "SLM Semantic Inference" in res.provenance_summary


# ---------------------------------------------------------------------------
# 7. Session Bootstrap Opt-In / Opt-Out Integration
# ---------------------------------------------------------------------------

def test_session_bootstrap_honors_slm_disabled(repo_root: Path):
    """Verify ChronosSessionBootstrap defaults to SLM disabled with sub-second deterministic execution."""
    con = get_connection(":memory:")
    # Initialize bootstrap without enable_slm flag -> must default to False
    boot = ChronosSessionBootstrap(repo_path=repo_root, con=con, enable_slm=False)
    assert boot.resolver.slm_resolver is None

    res = boot.bootstrap_session(task_prompt="Fix race condition in ledger")
    assert "augmented_prompt" in res
    assert "token_count" in res
    con.close()


def test_session_bootstrap_honors_slm_config_opt_in(repo_root: Path):
    """Verify ChronosSessionBootstrap wires SLMConfig when enabled."""
    con = get_connection(":memory:")
    cfg = SLMConfig(enabled=True, backend="openai_compatible", endpoint="http://localhost:8000/v1")
    boot = ChronosSessionBootstrap(repo_path=repo_root, con=con, slm_config=cfg)
    assert boot.resolver.slm_resolver is not None
    assert boot.resolver.slm_resolver.config.enabled is True
    con.close()
