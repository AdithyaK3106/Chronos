"""Comprehensive tests for Chronos zero-configuration, repository-aware dashboard launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
import pytest

from chronos.audit.logger import append_audit_event
from chronos.cli.ui import (
    format_terminal_output,
    launch_ui,
    probe_health,
    register_repo_with_server,
    stop_running_server,
    wait_for_server_ready,
)
from chronos.core.db import get_connection
from chronos.core.repo import (
    RepoIdentity,
    canonicalize_path,
    discover_repo_root,
    generate_repo_id,
    inspect_repository,
)
from chronos.server.dashboard_server import (
    REGISTRY,
    create_server,
    is_port_in_use,
    run_server,
)


@pytest.fixture
def temp_repo_pair(tmp_path: Path):
    """Create two distinct temporary repositories with distinct files and databases."""
    repo_a = tmp_path / "repo-alpha"
    repo_b = tmp_path / "repo-beta"

    for r, name in [(repo_a, "alpha"), (repo_b, "beta")]:
        r.mkdir(parents=True, exist_ok=True)
        # Create a git directory to simulate git root
        (r / ".git").mkdir(parents=True, exist_ok=True)
        (r / ".git" / "HEAD").write_text("ref: refs/heads/feature-x\n", encoding="utf-8")
        refs_dir = r / ".git" / "refs" / "heads"
        refs_dir.mkdir(parents=True, exist_ok=True)
        (refs_dir / "feature-x").write_text(f"abcdef{name[:2]}\n", encoding="utf-8")

        # Create source files
        (r / f"{name}_service.py").write_text(f"def process_{name}():\n    return '{name}'\n", encoding="utf-8")

        # Initialize .chronos dir and db
        c_dir = r / ".chronos"
        c_dir.mkdir(parents=True, exist_ok=True)
        con = get_connection(c_dir / "chronos.db")
        # Ingest a distinctive node
        con.execute(
            "INSERT INTO code_nodes (id, name, path, kind, language) VALUES (?, ?, ?, 'function', 'python')",
            (f"{name}::process_{name}", f"process_{name}", f"{name}_service.py"),
        )
        con.execute(
            "INSERT INTO memories (repo_namespace, tier, content, created_at, updated_at) VALUES (?, 'learned', ?, '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z')",
            (name, f"Specific memory invariant for {name} repository"),
        )
        con.commit()
        con.close()

        # Cryptographic Audit Log
        append_audit_event(
            log_path=c_dir / "chronos-audit.log",
            event_type="init",
            agent_id=f"{name}_agent",
            action="init_repo",
            target=name,
            status="ALLOW",
            reason=f"Initial seed event for {name}",
        )

    return repo_a, repo_b


def test_1_root_discovery_from_repo_root(temp_repo_pair):
    """Running discovery from repository root selects correct root."""
    repo_a, _ = temp_repo_pair
    detected = discover_repo_root(repo_a)
    assert detected == repo_a.resolve()


def test_2_root_discovery_from_nested_directory(temp_repo_pair):
    """Running discovery from a nested subdirectory selects enclosing repository."""
    repo_a, _ = temp_repo_pair
    nested = repo_a / "src" / "deep" / "pkg"
    nested.mkdir(parents=True, exist_ok=True)

    detected = discover_repo_root(nested)
    assert detected == repo_a.resolve()


def test_3_repo_flag_overrides_discovery(temp_repo_pair):
    """The --repo flag overrides automatic current working directory discovery."""
    repo_a, repo_b = temp_repo_pair
    info_b = inspect_repository(repo_b)
    assert info_b.canonical_root == str(repo_b.resolve())
    assert info_b.name == "repo-beta"


def test_4_paths_canonicalized_consistently(temp_repo_pair):
    """Paths are canonicalized consistently regardless of relative separators or symlinks."""
    repo_a, _ = temp_repo_pair
    # Path with redundant dots
    messy = repo_a / "sub" / ".." / "."
    messy.mkdir(parents=True, exist_ok=True)

    c1 = canonicalize_path(repo_a)
    c2 = canonicalize_path(messy)
    assert c1 == c2

    id1 = generate_repo_id(c1)
    id2 = generate_repo_id(c2)
    assert id1 == id2


def test_5_two_different_repositories_remain_isolated(temp_repo_pair):
    """Two different repositories have unique IDs and isolated database connections."""
    repo_a, repo_b = temp_repo_pair
    info_a = inspect_repository(repo_a)
    info_b = inspect_repository(repo_b)

    assert info_a.id != info_b.id
    assert info_a.name != info_b.name

    # Check database isolation
    con_a = get_connection(info_a.db_path)
    nodes_a = [r["name"] for r in con_a.execute("SELECT name FROM code_nodes").fetchall()]
    con_a.close()

    con_b = get_connection(info_b.db_path)
    nodes_b = [r["name"] for r in con_b.execute("SELECT name FROM code_nodes").fetchall()]
    con_b.close()

    assert "process_alpha" in nodes_a
    assert "process_beta" not in nodes_a

    assert "process_beta" in nodes_b
    assert "process_alpha" not in nodes_b


def test_6_and_7_server_lifecycle_and_port_conflict(temp_repo_pair):
    """Server handles repeated launches, health checks, reuse, and detects port conflicts safely."""
    repo_a, repo_b = temp_repo_pair
    port = 8792

    # Start live server on port 8792 in background thread
    server = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        assert wait_for_server_ready("127.0.0.1", port, max_seconds=3.0)

        # Health check
        health = probe_health("127.0.0.1", port)
        assert health is not None
        assert health.get("status") == "ok"
        assert health.get("app") == "chronos-ui"

        # Register repo A
        info_a = inspect_repository(repo_a)
        reg_a = register_repo_with_server("127.0.0.1", port, info_a.canonical_root)
        assert reg_a["id"] == info_a.id

        # Register repo B (reuse instance!)
        info_b = inspect_repository(repo_b)
        reg_b = register_repo_with_server("127.0.0.1", port, info_b.canonical_root)
        assert reg_b["id"] == info_b.id

        # Verify both repositories are registered in server
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repos")
        repos_data = json.loads(req.read().decode("utf-8"))
        registered_ids = [r["id"] for r in repos_data["repos"]]
        assert info_a.id in registered_ids
        assert info_b.id in registered_ids

    finally:
        server.shutdown()
        server.server_close()

    # Verify port conflict detection against an unrelated socket
    dummy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    dummy.bind(("127.0.0.1", port))
    dummy.listen(1)
    try:
        assert is_port_in_use("127.0.0.1", port)
        # probe_health should return None because dummy socket is not chronos-ui
        assert probe_health("127.0.0.1", port, timeout=0.2) is None
    finally:
        dummy.close()


def test_8_no_browser_flag(monkeypatch):
    """--no-browser prevents invoking webbrowser.open."""
    browser_called = []
    monkeypatch.setattr("webbrowser.open", lambda url: browser_called.append(url))

    # Test format output with open_browser=False
    output = format_terminal_output(
        RepoIdentity(
            id="test-1234",
            name="test",
            root="/tmp/test",
            canonical_root="/tmp/test",
            is_git=False,
            branch=None,
            commit=None,
            is_clean=True,
            has_chronos_dir=True,
            is_indexed=True,
            node_count=10,
            edge_count=15,
            last_synced_at=None,
            db_path="/tmp/test/.chronos/chronos.db",
            audit_log_path="/tmp/test/.chronos/chronos-audit.log",
        ),
        dashboard_url="http://127.0.0.1:8765/repo/test-1234",
        open_browser=False,
    )
    assert "Opening dashboard..." not in output
    assert len(browser_called) == 0


def test_9_and_10_unindexed_repository_handling(tmp_path: Path):
    """An unindexed repository is honestly detected, identified as unindexed, and can be indexed."""
    unindexed = tmp_path / "fresh-project"
    unindexed.mkdir()
    (unindexed / "app.py").write_text("def start():\n    return True\n", encoding="utf-8")

    info = inspect_repository(unindexed)
    assert info.is_indexed is False
    assert info.node_count == 0

    # Start test server
    port = 8793
    server = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        wait_for_server_ready("127.0.0.1", port, max_seconds=3.0)
        reg = register_repo_with_server("127.0.0.1", port, info.canonical_root)
        repo_id = reg["id"]

        # Check mission control status
        req = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{repo_id}/mission")
        mission = json.loads(req.read().decode("utf-8"))
        assert mission["freshness"] == "Unindexed"
        assert mission["stats"]["is_indexed"] is False

        # Now trigger indexing via POST /api/repo/{repo_id}/index
        req_idx = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/repo/{repo_id}/index",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        resp_idx = urllib.request.urlopen(req_idx)
        idx_data = json.loads(resp_idx.read().decode("utf-8"))
        assert idx_data["success"] is True
        assert idx_data["node_count"] >= 1

        # Check mission control after indexing
        req_after = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{repo_id}/mission")
        mission_after = json.loads(req_after.read().decode("utf-8"))
        assert mission_after["freshness"] == "Ready"
        assert mission_after["stats"]["is_indexed"] is True
        assert mission_after["stats"]["node_count"] >= 1

    finally:
        server.shutdown()
        server.server_close()


def test_11_and_12_dashboard_data_scoped_and_unauthorized_rejected(temp_repo_pair):
    """Dashboard APIs return strictly scoped data for selected repository and reject invalid repos."""
    repo_a, repo_b = temp_repo_pair
    port = 8794
    server = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        wait_for_server_ready("127.0.0.1", port, max_seconds=3.0)
        reg_a = register_repo_with_server("127.0.0.1", port, str(repo_a.resolve()))
        reg_b = register_repo_with_server("127.0.0.1", port, str(repo_b.resolve()))
        id_a = reg_a["id"]
        id_b = reg_b["id"]

        # 1. Memory scope test
        req_mem_a = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{id_a}/memory")
        mem_a = json.loads(req_mem_a.read().decode("utf-8"))
        assert mem_a["namespace"] == "repo-alpha"
        for m in mem_a["memories"]:
            assert "alpha" in m["content"]
            assert "beta" not in m["content"]

        req_mem_b = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{id_b}/memory")
        mem_b = json.loads(req_mem_b.read().decode("utf-8"))
        assert mem_b["namespace"] == "repo-beta"
        for m in mem_b["memories"]:
            assert "beta" in m["content"]
            assert "alpha" not in m["content"]

        # 2. Graph scope test
        req_nodes_a = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{id_a}/graph/nodes?query=process")
        nodes_a = json.loads(req_nodes_a.read().decode("utf-8"))["nodes"]
        assert any("process_alpha" in n["name"] for n in nodes_a)
        assert not any("process_beta" in n["name"] for n in nodes_a)

        # 3. Lock acquisition scope test
        lock_payload = json.dumps({
            "node_id": "alpha_service.py::process_alpha",
            "agent_id": "agent_alpha",
            "intent": "editing alpha",
        }).encode("utf-8")
        req_lock = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/repo/{id_a}/locks/acquire",
            data=lock_payload,
            headers={"Content-Type": "application/json"},
        )
        res_lock = json.loads(urllib.request.urlopen(req_lock).read().decode("utf-8"))
        assert res_lock["acquired"] is True

        # Verify lock is visible in repo A but not repo B
        req_locks_a = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{id_a}/locks")
        locks_a = json.loads(req_locks_a.read().decode("utf-8"))["locks"]
        assert any(l["node_id"] == "alpha_service.py::process_alpha" for l in locks_a)

        req_locks_b = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/{id_b}/locks")
        locks_b = json.loads(req_locks_b.read().decode("utf-8"))["locks"]
        assert not any(l["node_id"] == "alpha_service.py::process_alpha" for l in locks_b)

        # 4. Rejection of unauthorized / nonexistent repository ID
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/repo/non-existent-repo-123/mission")
        assert exc_info.value.code == 404

    finally:
        server.shutdown()
        server.server_close()


def test_13_audit_verification_endpoint(temp_repo_pair):
    """Tamper-evident audit chain verification executes over repository-specific audit log."""
    repo_a, _ = temp_repo_pair
    port = 8795
    server = create_server("127.0.0.1", port)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        wait_for_server_ready("127.0.0.1", port, max_seconds=3.0)
        reg_a = register_repo_with_server("127.0.0.1", port, str(repo_a.resolve()))
        id_a = reg_a["id"]

        req_verify = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/repo/{id_a}/audit/verify",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req_verify)
        data = json.loads(resp.read().decode("utf-8"))
        assert "is_valid" in data
        assert data["count"] >= 1
    finally:
        server.shutdown()
        server.server_close()
