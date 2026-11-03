"""Regression tests for the governance / memory / indexing bug-fix batch."""

from __future__ import annotations

import json

import pytest

from chronos.audit.logger import (
    AuditChainCorruptedError,
    append_audit_event,
    verify_audit_chain,
)
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.coordination.ledger import acquire_lock
from chronos.core.db import get_connection
from chronos.security.gates import grant_gate_approval
from chronos.security.permissions import set_permissions
from runtime.hooks import evaluate_tool_security
from runtime.session import ChronosSessionBootstrap


# --------------------------------------------------------------------------- #
# Bug 2: lock collisions between files that share a basename
# --------------------------------------------------------------------------- #
def test_lock_on_one_file_does_not_block_same_basename_elsewhere(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "utils.py").write_text("def f():\n    pass\n")
    (tmp_path / "b" / "utils.py").write_text("def g():\n    pass\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")

    nodes, _ = index_repository(tmp_path)
    locked = next(n for n in nodes if n.name == "f")
    assert acquire_lock(con, locked.id, "agent_a", "s1", "edit")["acquired"]

    # Other agent: same basename in another directory is free
    ok_b, _ = evaluate_tool_security(
        con, "agent_b", "file_editor",
        {"command": "str_replace", "path": "b/utils.py", "old_str": "x", "new_str": "y"},
        tmp_path,
    )
    assert ok_b

    # ...but the genuinely locked file is still protected
    ok_a, reason = evaluate_tool_security(
        con, "agent_b", "file_editor",
        {"command": "str_replace", "path": "a/utils.py", "old_str": "x", "new_str": "y"},
        tmp_path,
    )
    assert not ok_a
    assert "locked by agent 'agent_a'" in reason
    con.close()


# --------------------------------------------------------------------------- #
# Bug 3: protected-module gates must not block reads
# --------------------------------------------------------------------------- #
def test_gate_blocks_edits_but_not_views(tmp_path):
    (tmp_path / "auth.py").write_text("def login():\n    pass\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")

    ok_view, _ = evaluate_tool_security(
        con, "agent_a", "file_editor", {"command": "view", "path": "auth.py"}, tmp_path
    )
    assert ok_view

    ok_edit, reason = evaluate_tool_security(
        con, "agent_a", "file_editor",
        {"command": "str_replace", "path": "auth.py", "old_str": "pass", "new_str": "return 1"},
        tmp_path,
    )
    assert not ok_edit
    assert "gate approval" in reason

    grant_gate_approval(con, "agent_a", "auth.py")
    ok_after, _ = evaluate_tool_security(
        con, "agent_a", "file_editor",
        {"command": "str_replace", "path": "auth.py", "old_str": "pass", "new_str": "return 1"},
        tmp_path,
    )
    assert ok_after
    con.close()


def test_view_of_secret_files_is_still_denied(tmp_path):
    (tmp_path / ".env").write_text("TOKEN=abc\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    ok, _ = evaluate_tool_security(
        con, "agent_a", "file_editor", {"command": "view", "path": ".env"}, tmp_path
    )
    assert not ok
    con.close()


def test_unknown_command_is_treated_as_mutating(tmp_path):
    (tmp_path / "auth.py").write_text("x = 1\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    ok, _ = evaluate_tool_security(
        con, "agent_a", "file_editor", {"command": "mystery_op", "path": "auth.py"}, tmp_path
    )
    assert not ok
    con.close()


# --------------------------------------------------------------------------- #
# Bug 4: stale nodes / edge pile-up on repeated indexing
# --------------------------------------------------------------------------- #
def test_sync_repository_is_idempotent_and_drops_stale_symbols(tmp_path):
    src = tmp_path / "mod.py"
    src.write_text("def keep():\n    return helper()\n\ndef helper():\n    return 1\n\ndef doomed():\n    return 2\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    store = SQLiteGraphStore(con)

    nodes, edges = index_repository(tmp_path)
    store.sync_repository(nodes, edges)
    first_edges = con.execute("SELECT count(*) FROM code_edges").fetchone()[0]

    for _ in range(3):
        nodes, edges = index_repository(tmp_path)
        stats = store.sync_repository(nodes, edges)
        assert stats["edges_added"] == 0
        assert stats["edges_invalidated"] == 0
    assert con.execute("SELECT count(*) FROM code_edges").fetchone()[0] == first_edges

    # Remove a symbol and a call: node disappears, edge is closed (not deleted)
    src.write_text("def keep():\n    return 1\n\ndef helper():\n    return 1\n")
    nodes, edges = index_repository(tmp_path)
    stats = store.sync_repository(nodes, edges)
    assert stats["nodes_removed"] >= 1
    assert stats["edges_invalidated"] >= 1
    names = {r["name"] for r in con.execute("SELECT name FROM code_nodes").fetchall()}
    assert "doomed" not in names
    closed = con.execute("SELECT count(*) FROM code_edges WHERE invalid_at IS NOT NULL").fetchone()[0]
    assert closed >= 1  # history preserved
    con.close()


def test_sync_repository_purges_duplicate_active_edges(tmp_path):
    (tmp_path / "m.py").write_text("def a():\n    return b()\n\ndef b():\n    return 1\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    store = SQLiteGraphStore(con)
    nodes, edges = index_repository(tmp_path)
    # Legacy behaviour: re-ingesting appended a new row each time
    store.ingest(nodes, edges)
    store.ingest(nodes, edges)
    store.sync_repository(nodes, edges)
    dupes = con.execute(
        "SELECT count(*) FROM (SELECT src,dst,type FROM code_edges WHERE invalid_at IS NULL "
        "GROUP BY src,dst,type HAVING count(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0
    con.close()


def test_update_file_removes_deleted_symbols(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("def a():\n    return 1\n\ndef gone():\n    return 2\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    store = SQLiteGraphStore(con)
    nodes, edges = index_repository(tmp_path)
    store.sync_repository(nodes, edges)
    f.write_text("def a():\n    return 1\n")
    store.update_file(f)
    names = {r["name"] for r in con.execute("SELECT name FROM code_nodes").fetchall()}
    assert "gone" not in names and "a" in names
    con.close()


def test_bootstrap_can_skip_reindex_and_still_resolve_explicit_targets(tmp_path):
    (tmp_path / "svc.py").write_text("def charge_card():\n    return 1\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    nodes, edges = index_repository(tmp_path)
    SQLiteGraphStore(con).sync_repository(nodes, edges)

    boot = ChronosSessionBootstrap(repo_path=tmp_path, con=con)
    res = boot.bootstrap_session(
        task_prompt="polish things", target_files=["svc.py"], refresh_index=False
    )
    assert "charge_card" in res["symbols"]
    con.close()


# --------------------------------------------------------------------------- #
# Bug 1: unique task ids so post-task memory is recorded every time
# --------------------------------------------------------------------------- #
def test_new_task_ids_are_unique():
    pytest.importorskip("openhands.sdk")
    from runtime.openhands_adapter import new_task_id

    assert len({new_task_id() for _ in range(200)}) == 200


def test_record_task_outcome_records_each_distinct_task(tmp_path):
    pytest.importorskip("openhands.sdk")
    from chronos.core.config import ChronosConfig
    from runtime.openhands_adapter import ChronosAgentRuntime, new_task_id

    (tmp_path / "calc.py").write_text("def divide(a, b):\n    return a / b\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    cfg = ChronosConfig(repo_path=str(tmp_path), db_path=str(tmp_path / ".chronos" / "chronos.db"),
                        audit_log_path=str(tmp_path / ".chronos" / "audit.log"))
    rt = ChronosAgentRuntime(config=cfg, db_con=con)

    kwargs = dict(
        task_prompt="Fix ZeroDivisionError in divide", agent_id="a", outcome="failure",
        files_modified=["calc.py"], error_message="ZeroDivisionError: division by zero",
    )
    ids1 = rt.record_task_outcome(task_id=new_task_id(), **kwargs)
    ids2 = rt.record_task_outcome(task_id=new_task_id(), **kwargs)
    assert ids1 and ids2, "both distinct tasks must record memories"

    same = "fixed_id"
    first = rt.record_task_outcome(task_id=same, **kwargs)
    again = rt.record_task_outcome(task_id=same, **kwargs)
    assert first and again == [], "same task_id stays idempotent"
    con.close()


# --------------------------------------------------------------------------- #
# Bug 6: audit chain must not silently restart from genesis
# --------------------------------------------------------------------------- #
def test_corrupt_audit_tail_is_not_silently_reset(tmp_path):
    log = tmp_path / "audit.log"
    append_audit_event(log, "t", "a", "act", "x", "allow")
    append_audit_event(log, "t", "a", "act", "y", "allow")
    with open(log, "a", encoding="utf-8") as f:
        f.write("{this is not json\n")

    with pytest.raises(AuditChainCorruptedError):
        append_audit_event(log, "t", "a", "act", "z", "allow")
    ok, _, _ = verify_audit_chain(log)
    assert not ok


def test_unrecordable_allow_decision_fails_closed(tmp_path):
    (tmp_path / "m.py").write_text("x = 1\n")
    con = get_connection(tmp_path / ".chronos" / "chronos.db")
    log = tmp_path / ".chronos" / "audit.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text("garbage-not-json\n")
    ok, reason = evaluate_tool_security(
        con, "agent_a", "file_editor", {"command": "view", "path": "m.py"}, tmp_path, log_path=log
    )
    assert not ok and "Audit" in reason
    con.close()


def test_healthy_audit_chain_still_extends(tmp_path):
    log = tmp_path / "audit.log"
    for i in range(5):
        append_audit_event(log, "t", "a", "act", str(i), "allow")
    ok, _, count = verify_audit_chain(log)
    assert ok and count == 5
    json.loads(log.read_text().splitlines()[-1])  # last line well-formed


# --------------------------------------------------------------------------- #
# Resolver: strongest lexical (BM25) match must rank first
# --------------------------------------------------------------------------- #
def test_resolver_ranks_strongest_lexical_match_first(tmp_path):
    from chronos.context.resolver import DeterministicContextResolver

    (tmp_path / "auth.py").write_text(
        "def verify_jwt_token(raw):\n    return raw\n\n"
        "def get_access_token():\n    return 1\n\n"
        "def set_refresh_token():\n    return 2\n\n"
        "def test_token_roundtrip():\n    return 3\n"
    )
    con = get_connection(":memory:")
    store = SQLiteGraphStore(con)
    nodes, edges = index_repository(tmp_path)
    store.sync_repository(nodes, edges)

    res = DeterministicContextResolver(tmp_path, store).resolve("verify jwt token")
    assert res.candidates, "expected at least one candidate"
    assert res.candidates[0].name == "verify_jwt_token"
    con.close()


# --------------------------------------------------------------------------- #
# Gates match whole words, not substrings
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path, protected",
    [
        ("auth.py", True), ("src/authService.ts", True), ("oauth_client.py", True),
        ("api/permissions.py", True), (".env.local", True), ("prod.env", True),
        ("credentials.json", True), ("tests/test_auth.py", True),
        ("author.py", False), ("AUTHORS", False), ("docs/authors.md", False),
        ("environment.py", False), ("rate_limiter.py", False), ("src/user_service.py", False),
    ],
)
def test_default_gates_match_words(tmp_path, path, protected):
    from chronos.security.gates import check_gate_approval

    ok, _ = check_gate_approval(path, tmp_path)
    assert ok is (not protected)


def test_custom_protected_yml_keeps_glob_semantics_and_mcp_agrees(tmp_path):
    from chronos.security.gates import check_gate_approval, find_protected_module

    (tmp_path / ".chronos").mkdir()
    (tmp_path / ".chronos" / "protected.yml").write_text(
        "protected_modules:\n  - name: billing\n    paths: ['src/billing/*']\n"
    )
    assert check_gate_approval("src/billing/invoice.py", tmp_path)[0] is False
    assert find_protected_module("src/billing/invoice.py", tmp_path)[0]["name"] == "billing"
    assert find_protected_module("src/catalog/item.py", tmp_path) is None
