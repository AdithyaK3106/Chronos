"""Chronos Session Bootstrap and Automatic Knowledge Injection (Phase 2).

Guarantees:
- Indexes or refreshes repository AST into bi-temporal knowledge graph before inference.
- Resolves task target files and symbols to callers, callees, and transitive blast radius.
- Enforces token budgets on context envelopes.
- Acquires initial sub-file AST leases to prevent concurrent collisions.
- Delivers mandatory context directly to OpenHands agent before Turn 1 without model action.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from chronos.context.budgeter import assemble_context
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.context.resolver import DeterministicContextResolver, ResolutionResult
from chronos.coordination.ledger import acquire_lock
from chronos.security.gates import check_gate_approval
from chronos.security.permissions import check_action_allowed
from chronos.verification.rule_store import get_active_blocking_rules


class ChronosSessionBootstrap:
    """Coordinates deterministic pre-turn-1 knowledge injection and lease acquisition."""

    def __init__(self, repo_path: str | Path, con: sqlite3.Connection):
        self.repo_path = Path(repo_path).resolve()
        self.con = con
        self.graph_store = SQLiteGraphStore(con)
        self.resolver = DeterministicContextResolver(repo_root=self.repo_path, graph_store=self.graph_store)

    def bootstrap_session(
        self,
        task_prompt: str,
        target_files: Optional[List[str]] = None,
        agent_id: str = "default_agent",
        session_id: str = "default_session",
        token_budget: int = 1500,
    ) -> Dict[str, Any]:
        """Assemble bi-temporal AST graph context, rules, and leases before first model turn."""
        target_files = target_files or []

        # 1. Index repository AST and ingest into graph store
        try:
            nodes, edges = index_repository(self.repo_path)
            self.graph_store.ingest(nodes, edges)
        except Exception:
            nodes = []

        # 2. Deterministically resolve target files, symbols, and tests from prompt (Zero-Path)
        resolution: ResolutionResult = self.resolver.resolve(
            prompt=task_prompt,
            explicit_target_files=target_files if target_files else None,
            max_seeds=5,
        )

        resolved_symbols: List[str] = list(resolution.seed_symbols)
        resolved_files: List[str] = list(resolution.target_files)
        target_nodes: List[str] = [c.node_id for c in resolution.candidates if c.kind != "File"]
        test_files: List[str] = list(resolution.test_files)

        # Fallback to direct path search if zero-path resolution returned empty and explicit target_files were provided
        if not resolved_symbols and target_files:
            for n in nodes:
                norm_node_path = Path(n.path).as_posix()
                if any(Path(tf).as_posix() in norm_node_path or norm_node_path.endswith(Path(tf).as_posix()) for tf in target_files):
                    resolved_symbols.append(n.name)
                    target_nodes.append(n.id)

        # Adaptive Injection: detect whether user task targets specific files/functions
        is_targeted = bool(resolved_files) or bool(target_files) or bool(resolved_symbols) or any(
            tok.endswith((".py", ".ts", ".js", ".go", ".rs", ".java")) or "/" in tok
            for tok in task_prompt.split()
        )

        # 3. Assemble ranked bi-temporal graph context under token budget
        effective_targets = resolved_files or target_files
        ast_ctx = assemble_context(
            symbols=resolved_symbols[:12],
            graph_store=self.graph_store,
            token_budget=token_budget,
            is_targeted=is_targeted,
            target_files=effective_targets,
        )

        # 4. Retrieve active blocking rules
        try:
            rules = get_active_blocking_rules(self.con)
        except Exception:
            rules = []

        # 5. Acquire initial AST node leases for target nodes ONLY if authorized/approved
        acquired_leases: List[str] = []
        for node_id in target_nodes[:5]:
            # Extract path from node_id (format: path::name::kind)
            node_path = node_id.split("::")[0] if "::" in node_id else ""
            if node_path:
                is_approved, _ = check_gate_approval(
                    node_path, self.repo_path, con=self.con, agent_id=agent_id
                )
                if not is_approved:
                    # Decouple retrieval from authorization: do NOT grant write leases on gated files
                    continue
                is_allowed, _ = check_action_allowed(
                    con=self.con,
                    agent_id=agent_id,
                    tool_name="file_editor",
                    target_path=node_path,
                    repo_root=self.repo_path,
                )
                if not is_allowed:
                    # Do NOT grant write lease if path permissions are denied
                    continue

            try:
                res = acquire_lock(
                    con=self.con,
                    node_id=node_id,
                    agent_id=agent_id,
                    session_id=session_id,
                    intent=f"Initial task lease for {agent_id}",
                    ttl=300,
                )
                if res.get("acquired") or res.get("renewed"):
                    acquired_leases.append(node_id)
            except Exception:
                pass

        # 6. Format mandatory context envelope
        envelope_lines = [
            ast_ctx["text"],
            "",
            resolution.provenance_summary,
            "",
            "#### 🛡 Active Governance Invariants & Blocking Rules:",
        ]
        if rules:
            for r in rules:
                pat = r.get("yaml_pattern") or r.get("rule_text", "")
                envelope_lines.append(f"- Rule `{r['rule_id']}`: `{pat}` (Severity: {r.get('status', 'blocking')})")
        else:
            envelope_lines.append("- Standard deterministic governance active. Secrets and path escapes strictly denied.")

        if acquired_leases:
            envelope_lines.append("")
            envelope_lines.append(f"#### 🔒 Active Chronos Sub-File AST Leases ({len(acquired_leases)} held by {agent_id}):")
            for l in acquired_leases:
                envelope_lines.append(f"- Held lease: `{l}`")

        envelope_text = "\n".join(envelope_lines)
        augmented_prompt = f"{task_prompt}\n\n{envelope_text}"

        return {
            "envelope_text": envelope_text,
            "augmented_prompt": augmented_prompt,
            "token_count": ast_ctx["token_count"],
            "symbols": ast_ctx["included_symbols"],
            "leases": acquired_leases,
            "rules_count": len(rules),
            "resolved_files": resolved_files,
            "resolved_symbols": resolved_symbols,
            "test_files": test_files,
            "candidates": resolution.candidates,
            "provenance_summary": resolution.provenance_summary,
        }
