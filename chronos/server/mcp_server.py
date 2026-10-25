"""Unified FastMCP 3.x server exposing Chronos tools to external coding agents."""

from pathlib import Path
from typing import Any
from fastmcp import FastMCP

from chronos.audit.logger import append_audit_event
from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.coordination.ledger import (
    acquire_lock,
    check_conflicts,
    list_active_locks,
    release_lock,
)
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection

cfg = ChronosConfig.load()

mcp = FastMCP("chronos")

def _get_root() -> Path:
    return Path(cfg.repo_path).resolve()

def _get_con():
    root = _get_root()
    db_file = root / cfg.db_path
    return get_connection(db_file)

def _get_audit_log() -> Path:
    root = _get_root()
    return root / cfg.audit_log_path

@mcp.tool()
def as_of_callers(symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
    """Return callers of a function or class as of a specific point in time."""
    con = _get_con()
    try:
        store = SQLiteGraphStore(con)
        return store.as_of_callers(symbol_name, as_of=as_of)
    finally:
        con.close()

@mcp.tool()
def as_of_callees(symbol_name: str, as_of: str | None = None) -> list[dict[str, Any]]:
    """Return functions called by a symbol as of a specific point in time."""
    con = _get_con()
    try:
        store = SQLiteGraphStore(con)
        return store.as_of_callees(symbol_name, as_of=as_of)
    finally:
        con.close()

@mcp.tool()
def as_of_impact(symbol_name: str, as_of: str | None = None, max_depth: int = 3) -> list[dict[str, Any]]:
    """Compute downstream blast radius / transitive callers of modifying a symbol."""
    con = _get_con()
    try:
        store = SQLiteGraphStore(con)
        return store.as_of_impact(symbol_name, as_of=as_of, max_depth=max_depth)
    finally:
        con.close()

@mcp.tool()
def as_of_diff(t1: str, t2: str) -> dict[str, Any]:
    """Compute structural AST differences between two points in time."""
    con = _get_con()
    try:
        store = SQLiteGraphStore(con)
        return store.as_of_diff(t1, t2)
    finally:
        con.close()

@mcp.tool()
def chronos_acquire_lock(
    node_id: str,
    agent_id: str,
    session_id: str,
    intent: str,
    ttl: int = 300,
) -> dict[str, Any]:
    """Acquire an atomic lease on a sub-file AST node to prevent multi-agent collisions."""
    con = _get_con()
    try:
        result = acquire_lock(con, node_id, agent_id, session_id, intent, ttl=ttl)
        append_audit_event(
            log_path=_get_audit_log(),
            event_type="lock_acquire",
            agent_id=agent_id,
            action="acquire_lock",
            target=node_id,
            status="allow" if result.get("acquired") else "deny",
            reason=intent if result.get("acquired") else "conflict",
        )
        return result
    finally:
        con.close()

@mcp.tool()
def chronos_release_lock(
    node_id: str,
    agent_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Release a held AST node lock."""
    con = _get_con()
    try:
        result = release_lock(con, node_id, agent_id, session_id)
        append_audit_event(
            log_path=_get_audit_log(),
            event_type="lock_release",
            agent_id=agent_id,
            action="release_lock",
            target=node_id,
            status="allow" if result.get("released") else "deny",
            reason=result.get("reason", "released"),
        )
        return result
    finally:
        con.close()

@mcp.tool()
def chronos_check_conflicts(node_ids: list[str]) -> dict[str, Any]:
    """Check lock status for a list of AST node IDs."""
    con = _get_con()
    try:
        return check_conflicts(con, node_ids)
    finally:
        con.close()

@mcp.tool()
def chronos_list_locks() -> list[dict[str, Any]]:
    """List all currently active non-expired AST node locks."""
    con = _get_con()
    try:
        return list_active_locks(con)
    finally:
        con.close()

@mcp.tool()
def chronos_assemble_context(symbols: list[str], token_budget: int = 4000) -> dict[str, Any]:
    """Assemble graph-aware repository context within a token budget."""
    con = _get_con()
    try:
        store = SQLiteGraphStore(con)
        return assemble_context(symbols, store, token_budget=token_budget)
    finally:
        con.close()

@mcp.tool()
def chronos_resolve_zero_path_context(prompt: str, token_budget: int = 2000) -> dict[str, Any]:
    """Deterministically resolve repository AST context, provenance, and tests from natural-language prompt."""
    con = _get_con()
    try:
        from runtime.session import ChronosSessionBootstrap
        boot = ChronosSessionBootstrap(repo_path=_get_root(), con=con)
        res = boot.bootstrap_session(task_prompt=prompt, token_budget=token_budget)
        return {
            "resolved_files": res.get("resolved_files", []),
            "resolved_symbols": res.get("resolved_symbols", []),
            "test_files": res.get("test_files", []),
            "provenance_summary": res.get("provenance_summary", ""),
            "envelope_text": res.get("envelope_text", ""),
            "token_count": res.get("token_count", 0),
        }
    finally:
        con.close()

def main():
    mcp.run()

if __name__ == "__main__":
    main()
