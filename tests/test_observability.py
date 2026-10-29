"""Tests for Chronos Observability Suite:
- Feature 1: Runs & Trace Explorer (Persistent registry, structured events, secret scrubbing)
- Feature 2: Context Inspector (Context capture, candidate ranking signals, envelope diffing)
- Feature 3: Evaluation Studio (Paired benchmark metrics, regression analysis, CSV/JSON export)
- Integration with Dashboard HTTP Server API.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
import socket
import threading
import time
import urllib.error
import urllib.request
import pytest

from chronos.core.db import get_connection
from chronos.core.repo import inspect_repository
from chronos.server.dashboard_server import REGISTRY, create_server
from chronos.telemetry.runs import (
    capture_context_envelope,
    create_run,
    diff_context_envelopes,
    export_evaluation,
    get_context_envelope,
    get_evaluation_detail,
    get_evaluation_experiments,
    get_run,
    list_context_envelopes,
    list_runs,
    log_run_event,
    sync_benchmark_runs_into_registry,
    update_run,
)


@pytest.fixture
def memory_db():
    """In-memory SQLite connection configured with the unified Chronos schema."""
    con = get_connection(":memory:")
    yield con
    con.close()


@pytest.fixture
def sample_repo(tmp_path: Path):
    """Temporary indexed repository for integration tests."""
    repo_dir = tmp_path / "obs_test_repo"
    repo_dir.mkdir()
    (repo_dir / "app.py").write_text(
        "def compute_result(x: int) -> int:\n    return x * 2\n\ndef main():\n    return compute_result(10)\n",
        encoding="utf-8",
    )
    (repo_dir / "test_app.py").write_text(
        "from app import compute_result\ndef test_compute():\n    assert compute_result(5) == 10\n",
        encoding="utf-8",
    )
    from chronos.context.indexer import index_repository
    from chronos.context.graph import SQLiteGraphStore
    nodes, edges = index_repository(repo_dir)

    identity = inspect_repository(repo_dir)
    con = get_connection(identity.db_path)
    store = SQLiteGraphStore(con)
    store.ingest(nodes, edges)
    con.close()

    REGISTRY.register(repo_dir)

    # Symlink benchmarks/data for evaluation testing
    real_benchmarks = Path(__file__).resolve().parent.parent / "benchmarks"
    if real_benchmarks.is_dir():
        import os
        benchmarks_link = repo_dir / "benchmarks"
        try:
            os.symlink(real_benchmarks, benchmarks_link)
        except Exception:
            pass

    return identity


# =========================================================================
# Feature 1: Persistent Run Registry & Structured Trace Events Tests
# =========================================================================

def test_run_creation_and_initial_event(memory_db):
    """Creating a run persists it and automatically logs the initial 'run_started' event."""
    run = create_run(
        con=memory_db,
        repo_id="repo-1",
        task_id="task_01",
        task_description="Fix race condition in distributed lock ledger",
        agent_id="agent-007",
        model_id="gemini-3.8-flash-low",
        provider_name="agy",
        arm="treatment",
        suite="efficacy",
        metadata={"tags": ["concurrency", "locks"]},
    )

    assert run["run_id"].startswith("run_")
    assert run["repo_id"] == "repo-1"
    assert run["task_id"] == "task_01"
    assert run["status"] == "RUNNING"
    assert run["arm"] == "treatment"
    assert run["suite"] == "efficacy"
    assert run["metadata"]["tags"] == ["concurrency", "locks"]

    # Verify event waterfall contains initial run_started event
    full_run = get_run(memory_db, run["run_id"], include_events=True)
    assert full_run is not None
    assert len(full_run["events"]) == 1
    assert full_run["events"][0]["event_type"] == "run_started"
    assert full_run["events"][0]["seq"] == 1


def test_monotonic_event_sequencing(memory_db):
    """Events appended to a run receive strictly monotonic sequence numbers."""
    run = create_run(con=memory_db, repo_id="repo-1", task_id="task_seq")
    run_id = run["run_id"]

    e1 = log_run_event(memory_db, run_id, "intent_resolution", {"intent": "find_symbol"})
    e2 = log_run_event(memory_db, run_id, "file_retrieval", {"files": ["app.py"]})
    e3 = log_run_event(memory_db, run_id, "tool_invocation", {"tool": "edit_file"})
    e4 = log_run_event(memory_db, run_id, "tool_completion", {"tool": "edit_file", "exit_code": 0})

    # seq 1 was run_started
    assert e1["seq"] == 2
    assert e2["seq"] == 3
    assert e3["seq"] == 4
    assert e4["seq"] == 5

    full_run = get_run(memory_db, run_id, include_events=True)
    assert [ev["seq"] for ev in full_run["events"]] == [1, 2, 3, 4, 5]


def test_secret_scrubbing_in_events_and_runs(memory_db):
    """Sensitive API keys, passwords, and tokens are scrubbed from run metadata and events."""
    secret_meta = {
        "api_key": "sk-123456789012345678901234567890",
        "nested": {"password": "SuperSecretPassword123", "normal": "safe_value"},
    }
    run = create_run(
        con=memory_db,
        repo_id="repo-1",
        task_id="task_secret",
        metadata=secret_meta,
    )
    run_id = run["run_id"]

    # Check run metadata is scrubbed
    fetched_run = get_run(memory_db, run_id, include_events=False)
    assert fetched_run["metadata"]["api_key"] == "sk-[REDACTED]"
    assert fetched_run["metadata"]["nested"]["password"] == "[REDACTED]"
    assert fetched_run["metadata"]["nested"]["normal"] == "safe_value"

    # Log event with secret payload
    evt = log_run_event(
        memory_db,
        run_id,
        "tool_invocation",
        {
            "headers": {"Authorization": "Bearer chron_secrettoken12345678901234567890"},
            "log": "Connected with sk-12345678901234567890 and token chron_secrettoken12345678901234567890",
        },
    )
    # Sensitive Authorization header contains redacted token
    assert "[REDACTED]" in evt["payload"]["headers"]["Authorization"]
    # Content-based pattern redacting in ordinary text
    assert "sk-[REDACTED]" in evt["payload"]["log"]
    assert "chron_[REDACTED]" in evt["payload"]["log"]


def test_run_update_and_completion_event(memory_db):
    """Updating a run with terminal status automatically logs a terminal event."""
    run = create_run(con=memory_db, repo_id="repo-1", task_id="task_term")
    run_id = run["run_id"]

    updated = update_run(
        con=memory_db,
        run_id=run_id,
        status="SUCCESS",
        duration_seconds=12.45,
        prompt_tokens=1500,
        completion_tokens=250,
        cost_usd=0.0035,
    )
    assert updated["status"] == "SUCCESS"
    assert updated["duration_seconds"] == 12.45
    assert updated["total_tokens"] == 1750
    assert updated["cost_usd"] == 0.0035

    full = get_run(memory_db, run_id, include_events=True)
    last_event = full["events"][-1]
    assert last_event["event_type"] == "run_success"
    assert last_event["payload"]["status"] == "SUCCESS"


def test_list_runs_filtering_and_pagination(memory_db):
    """Runs list supports filtering by status, arm, suite, search, and pagination."""
    create_run(memory_db, repo_id="repo-1", task_id="t1", arm="treatment", suite="efficacy")
    create_run(memory_db, repo_id="repo-1", task_id="t2", arm="baseline", suite="efficacy")
    r3 = create_run(memory_db, repo_id="repo-1", task_id="t3", arm="treatment", suite="security")
    update_run(memory_db, r3["run_id"], status="FAILED")

    # Filter by arm
    treat_runs = list_runs(memory_db, repo_id="repo-1", arm="treatment")
    assert treat_runs["total"] == 2

    # Filter by status
    failed_runs = list_runs(memory_db, repo_id="repo-1", status="FAILED")
    assert failed_runs["total"] == 1
    assert failed_runs["runs"][0]["task_id"] == "t3"

    # Filter by suite
    sec_runs = list_runs(memory_db, repo_id="repo-1", suite="security")
    assert sec_runs["total"] == 1

    # Search
    search_res = list_runs(memory_db, repo_id="repo-1", search="t2")
    assert search_res["total"] == 1
    assert search_res["runs"][0]["task_id"] == "t2"

    # Pagination
    paged = list_runs(memory_db, repo_id="repo-1", limit=1, offset=1)
    assert len(paged["runs"]) == 1
    assert paged["limit"] == 1
    assert paged["offset"] == 1


# =========================================================================
# Feature 2: Context Inspector & Envelope Diffing Tests
# =========================================================================

def test_capture_and_retrieve_context_envelope(memory_db):
    """Context envelopes are persisted with candidate signals, provenance, and tokens."""
    candidates = [
        {
            "node_id": "func_compute",
            "name": "compute_result",
            "path": "app.py",
            "kind": "Function",
            "score": 38.5,
            "reasons": ["Exact symbol match for 'compute_result'", "Path stem match 'app'"],
            "signals": [{"signal": "Exact symbol", "weight": 25.0}],
            "source": "AST Graph",
        }
    ]
    env = capture_context_envelope(
        con=memory_db,
        repo_id="repo-1",
        prompt="Fix compute_result return value",
        resolved_files=["app.py"],
        resolved_symbols=["compute_result"],
        test_files=["test_app.py"],
        candidates=candidates,
        token_budget=2000,
        actual_tokens=140,
        provenance_summary="AST seed symbol 'compute_result' mapped to 'app.py'",
        mode="Deterministic AST + BM25",
    )

    env_id = env["envelope_id"]
    assert env_id.startswith("ctx_")

    fetched = get_context_envelope(memory_db, env_id)
    assert fetched is not None
    assert fetched["envelope_id"] == env_id
    assert fetched["resolved_files"] == ["app.py"]
    assert fetched["resolved_symbols"] == ["compute_result"]
    assert fetched["test_files"] == ["test_app.py"]
    assert fetched["token_budget"] == 2000
    assert fetched["actual_tokens"] == 140
    assert fetched["candidates"][0]["name"] == "compute_result"
    assert fetched["candidates"][0]["score"] == 38.5
    assert fetched["candidates"][0]["source"] == "AST Graph"


def test_context_envelope_diff(memory_db):
    """Diffing two context envelopes accurately computes additions, removals, and score changes."""
    cands_a = [
        {"node_id": "c1", "name": "alpha", "path": "alpha.py", "score": 20.0},
        {"node_id": "c2", "name": "beta", "path": "beta.py", "score": 15.0},
    ]
    cands_b = [
        {"node_id": "c2", "name": "beta", "path": "beta.py", "score": 25.0},  # Modified score
        {"node_id": "c3", "name": "gamma", "path": "gamma.py", "score": 30.0}, # Added candidate
    ]

    env_a = capture_context_envelope(
        con=memory_db,
        repo_id="repo-1",
        prompt="Turn 1 prompt",
        resolved_files=["alpha.py", "beta.py"],
        resolved_symbols=["alpha", "beta"],
        test_files=["test_alpha.py"],
        candidates=cands_a,
        actual_tokens=100,
    )
    env_b = capture_context_envelope(
        con=memory_db,
        repo_id="repo-1",
        prompt="Turn 2 prompt",
        resolved_files=["beta.py", "gamma.py"],
        resolved_symbols=["beta", "gamma"],
        test_files=["test_gamma.py"],
        candidates=cands_b,
        actual_tokens=150,
    )

    diff = diff_context_envelopes(env_a, env_b)

    # Token differences
    assert diff["tokens"]["base"] == 100
    assert diff["tokens"]["target"] == 150
    assert diff["tokens"]["delta"] == 50

    # File differences
    assert diff["files"]["added"] == ["gamma.py"]
    assert diff["files"]["removed"] == ["alpha.py"]
    assert diff["files"]["retained"] == ["beta.py"]

    # Symbol differences
    assert diff["symbols"]["added"] == ["gamma"]
    assert diff["symbols"]["removed"] == ["alpha"]
    assert diff["symbols"]["retained"] == ["beta"]

    # Test differences
    assert diff["test_files"]["added"] == ["test_gamma.py"]
    assert diff["test_files"]["removed"] == ["test_alpha.py"]

    # Candidate score diffs
    score_diffs = {c["name"]: c for c in diff["candidate_score_diffs"]}
    assert score_diffs["beta"]["status"] == "MODIFIED"
    assert score_diffs["beta"]["score_delta"] == 10.0
    assert score_diffs["gamma"]["status"] == "ADDED"
    assert score_diffs["alpha"]["status"] == "REMOVED"


def test_context_linked_to_run_emits_event(memory_db):
    """Capturing an envelope with a run_id automatically logs a 'context_envelope_created' event."""
    run = create_run(con=memory_db, repo_id="repo-1", task_id="task_ctx_link")
    run_id = run["run_id"]

    env = capture_context_envelope(
        con=memory_db,
        repo_id="repo-1",
        prompt="Linked envelope prompt",
        resolved_files=["app.py"],
        resolved_symbols=["compute_result"],
        test_files=[],
        candidates=[],
        run_id=run_id,
    )

    full_run = get_run(memory_db, run_id, include_events=True)
    event_types = [e["event_type"] for e in full_run["events"]]
    assert "context_envelope_created" in event_types

    # Envelope should be listed in run's context envelopes
    assert len(full_run["context_envelopes"]) == 1
    assert full_run["context_envelopes"][0]["envelope_id"] == env["envelope_id"]


# =========================================================================
# Feature 3: Evaluation Studio Tests
# =========================================================================

def test_evaluation_experiments_discovery():
    """Canonical benchmark summaries and database runs are discovered and listed."""
    benchmarks_dir = Path(__file__).resolve().parent.parent / "benchmarks" / "data"
    exps = get_evaluation_experiments(benchmarks_dir)
    assert len(exps) > 0
    exp_ids = [e["id"] for e in exps]
    assert "canonical_benchmark_runs" in exp_ids or any(id.startswith("summary_") for id in exp_ids)


def test_evaluation_detail_and_paired_metrics():
    """Paired evaluation metrics compute token savings, latency percentiles, and regressions."""
    benchmarks_dir = Path(__file__).resolve().parent.parent / "benchmarks" / "data"
    detail = get_evaluation_detail(benchmarks_dir, "canonical_benchmark_runs")
    assert detail is not None
    assert detail["paired_tasks_count"] >= 20

    # Token savings calculation
    assert detail["token_stats"]["overall_token_saving_pct"] > 50.0

    # Latency percentiles
    assert detail["latency_stats"]["baseline"]["p50"] is not None
    assert detail["latency_stats"]["treatment"]["p50"] is not None

    # Paired tasks classification
    for p in detail["paired_tasks"]:
        assert p["classification"] in ("BOTH_SUCCESS", "IMPROVEMENT", "REGRESSION", "BOTH_FAILED")
        assert "baseline" in p
        assert "treatment" in p


def test_evaluation_export_csv_and_json():
    """Evaluation export generates valid CSV and JSON payloads."""
    benchmarks_dir = Path(__file__).resolve().parent.parent / "benchmarks" / "data"

    # CSV Export
    csv_content, c_type = export_evaluation(benchmarks_dir, "canonical_benchmark_runs", fmt="csv")
    assert c_type == "text/csv"
    reader = csv.reader(csv_content.splitlines())
    header = next(reader)
    assert "task_id" in header
    assert "token_savings_pct" in header
    assert "duration_delta_sec" in header
    rows = list(reader)
    assert len(rows) >= 20

    # JSON Export
    json_content, j_type = export_evaluation(benchmarks_dir, "canonical_benchmark_runs", fmt="json")
    assert j_type == "application/json"
    parsed = json.loads(json_content)
    assert "paired_tasks" in parsed
    assert len(parsed["paired_tasks"]) >= 20


def test_sync_benchmark_runs_into_registry(memory_db):
    """Syncing benchmark database ingests historical runs and synthetic trace events."""
    benchmarks_dir = Path(__file__).resolve().parent.parent / "benchmarks" / "data"
    cnt = sync_benchmark_runs_into_registry(memory_db, "repo-test", benchmarks_dir)
    assert cnt > 0

    runs = list_runs(memory_db, repo_id="repo-test", limit=100)
    assert runs["total"] == cnt

    # Verify a synced run has events waterfall
    first_run_id = runs["runs"][0]["run_id"]
    r_detail = get_run(memory_db, first_run_id, include_events=True)
    assert len(r_detail["events"]) >= 2
    event_types = [e["event_type"] for e in r_detail["events"]]
    assert "run_started" in event_types


# =========================================================================
# Integration: Live HTTP Server API Endpoints
# =========================================================================

@pytest.fixture(scope="module")
def live_server():
    """Launch a temporary HTTP server on an available test port."""
    port = 8795
    server = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.3)
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


def test_api_runs_crud_lifecycle(live_server, sample_repo):
    """End-to-end HTTP API lifecycle: create run, log event, finish run, and inspect trace."""
    repo_id = sample_repo.id

    # 1. POST create run
    create_payload = json.dumps({
        "task_id": "test_api_task",
        "task_description": "API Integration test task",
        "model_id": "gemini-3.8-flash-low",
        "arm": "treatment",
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{live_server}/api/repo/{repo_id}/runs",
        data=create_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 201
        run_data = json.loads(resp.read().decode("utf-8"))
        run_id = run_data["run_id"]

    # 2. POST append event
    event_payload = json.dumps({
        "event_type": "tool_invocation",
        "payload": {"tool": "file_reader", "path": "app.py"},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{live_server}/api/repo/{repo_id}/runs/{run_id}/events",
        data=event_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 201

    # 3. POST finish run
    finish_payload = json.dumps({
        "status": "SUCCESS",
        "duration_seconds": 15.2,
        "total_tokens": 1200,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{live_server}/api/repo/{repo_id}/runs/{run_id}/finish",
        data=finish_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200

    # 4. GET run detail with waterfall
    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/runs/{run_id}") as resp:
        assert resp.status == 200
        detail = json.loads(resp.read().decode("utf-8"))
        assert detail["status"] == "SUCCESS"
        assert len(detail["events"]) == 3  # run_started, tool_invocation, run_success


def test_api_resolve_and_context_inspection(live_server, sample_repo):
    """Resolving context via API captures an envelope with genuine signals and returns envelope_id."""
    repo_id = sample_repo.id

    resolve_payload = json.dumps({
        "prompt": "Fix compute_result in app.py",
        "token_budget": 1800,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{live_server}/api/repo/{repo_id}/resolve",
        data=resolve_payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        res_data = json.loads(resp.read().decode("utf-8"))
        env_id = res_data.get("envelope_id")
        assert env_id is not None
        assert env_id.startswith("ctx_")

    # Inspect envelope via Context Inspector API
    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/context/{env_id}") as resp:
        assert resp.status == 200
        env = json.loads(resp.read().decode("utf-8"))
        assert env["envelope_id"] == env_id
        assert "app.py" in env["resolved_files"]
        assert len(env["candidates"]) > 0
        cand = env["candidates"][0]
        assert "score" in cand
        assert "signals" in cand
        assert "source" in cand


def test_api_context_diff_endpoint(live_server, sample_repo):
    """Context diff API compares two envelopes and returns differences."""
    repo_id = sample_repo.id

    # Create two envelopes
    p1 = json.dumps({"prompt": "Compute result test"}).encode("utf-8")
    req1 = urllib.request.Request(f"{live_server}/api/repo/{repo_id}/resolve", data=p1, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req1) as r:
        env1_id = json.loads(r.read().decode("utf-8"))["envelope_id"]

    p2 = json.dumps({"prompt": "Run main entrypoint"}).encode("utf-8")
    req2 = urllib.request.Request(f"{live_server}/api/repo/{repo_id}/resolve", data=p2, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req2) as r:
        env2_id = json.loads(r.read().decode("utf-8"))["envelope_id"]

    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/context/diff?base={env1_id}&target={env2_id}") as resp:
        assert resp.status == 200
        diff = json.loads(resp.read().decode("utf-8"))
        assert diff["base_envelope_id"] == env1_id
        assert diff["target_envelope_id"] == env2_id
        assert "files" in diff
        assert "tokens" in diff


def test_api_evaluation_studio_endpoints(live_server, sample_repo):
    """Evaluation Studio API serves experiments list, regression detail, and CSV export."""
    repo_id = sample_repo.id

    # List evaluations
    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/evaluations") as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "experiments" in data

    # Detail of canonical evaluation
    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/evaluations/canonical_benchmark_runs") as resp:
        assert resp.status == 200
        detail = json.loads(resp.read().decode("utf-8"))
        assert detail["paired_tasks_count"] >= 20
        assert "latency_stats" in detail

    # CSV Export
    with urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/evaluations/canonical_benchmark_runs/export?format=csv") as resp:
        assert resp.status == 200
        assert "text/csv" in resp.headers.get("Content-Type")
        csv_text = resp.read().decode("utf-8")
        assert "task_id" in csv_text


def test_api_negative_cases(live_server, sample_repo):
    """Negative tests: 404 for missing runs/envelopes, 400 for bad input."""
    repo_id = sample_repo.id

    # 404 for missing run
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/runs/run_does_not_exist")
    assert exc_info.value.code == 404

    # 404 for missing envelope
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/context/ctx_does_not_exist")
    assert exc_info.value.code == 404

    # 400 for diff missing parameters
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"{live_server}/api/repo/{repo_id}/context/diff")
    assert exc_info.value.code == 400

    # 400 for empty event_type
    bad_event = json.dumps({"event_type": "", "payload": {}}).encode("utf-8")
    req = urllib.request.Request(
        f"{live_server}/api/repo/{repo_id}/runs/run_123/events",
        data=bad_event,
        headers={"Content-Type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
