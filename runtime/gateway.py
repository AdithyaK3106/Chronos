"""Mandatory Execution-Boundary Tool Gateway for Chronos (Phase 3).

Guarantees:
- Wraps all registered OpenHands SDK tools with deterministic Chronos governance.
- Evaluates security permissions, path traversals, gates, AST locks, and rules.
- Fails closed: Denies execution if unverified, hook bypassed, or exception occurs.
- Implements both sync __call__ and async acall tool execution interfaces.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from chronos.audit.logger import append_audit_event
from runtime.hooks import evaluate_tool_security


class ChronosSecurityVetoError(ValueError):
    """Raised when an action is deterministically vetoed by Chronos governance."""
    pass


class ChronosToolGateway:
    """Execution-boundary proxy wrapping an OpenHands Tool callable."""

    def __init__(
        self,
        underlying_tool: Any,
        con: sqlite3.Connection,
        agent_id: str,
        repo_root: str | Path,
        log_path: Optional[str | Path] = None,
    ):
        self.underlying_tool = underlying_tool
        self.con = con
        self.agent_id = agent_id
        self.repo_root = Path(repo_root).resolve()
        self.log_path = log_path

        # Preserve OpenHands Tool metadata attributes
        self.name = getattr(underlying_tool, "name", "governed_tool")
        self.description = getattr(underlying_tool, "description", "")
        for attr in ("action_type", "declared_resources", "model_json_schema", "schema", "user_selectable"):
            if hasattr(underlying_tool, attr):
                setattr(self, attr, getattr(underlying_tool, attr))

    def __getattr__(self, item: str) -> Any:
        return getattr(self.underlying_tool, item)

    def _evaluate(self, action: Any) -> None:
        """Evaluate action against Chronos security policies. Fails closed."""
        tool_name = getattr(self.underlying_tool, "name", "unknown")
        tool_input: Dict[str, Any] = {}
        if hasattr(action, "model_dump"):
            try:
                tool_input = action.model_dump()
            except Exception:
                tool_input = {}
        elif isinstance(action, dict):
            tool_input = action

        allowed, reason = evaluate_tool_security(
            con=self.con,
            agent_id=self.agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            repo_root=self.repo_root,
            log_path=self.log_path,
        )

        if not allowed:
            raise ChronosSecurityVetoError(f"[Chronos Veto] Action blocked by Chronos policy: {reason}")

    def _post_execute_sync(self, action: Any) -> None:
        """P2: Wire incremental AST parser post-execution to synchronize bi-temporal graph."""
        try:
            tool_name = getattr(self.underlying_tool, "name", "unknown")
            if tool_name in ("file_editor", "edit_file", "write_file"):
                tool_input = {}
                if hasattr(action, "model_dump"):
                    tool_input = action.model_dump()
                elif isinstance(action, dict):
                    tool_input = action
                from runtime.hooks import _extract_target_path
                target = _extract_target_path(tool_name, tool_input)
                from chronos.context.indexer import is_supported_source_file
                if target and is_supported_source_file(target):
                    full_path = (self.repo_root / target).resolve()
                    if full_path.is_file():
                        from chronos.context.graph import SQLiteGraphStore
                        store = SQLiteGraphStore(self.con)
                        store.update_file(full_path)
        except Exception:
            pass

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Synchronous tool invocation interceptor."""
        action = args[0] if args else kwargs.get("action")
        self._evaluate(action)
        res = self.underlying_tool(*args, **kwargs)
        self._post_execute_sync(action)
        return res

    async def acall(self, *args: Any, **kwargs: Any) -> Any:
        """Asynchronous tool invocation interceptor."""
        action = args[0] if args else kwargs.get("action")
        self._evaluate(action)
        if hasattr(self.underlying_tool, "acall"):
            res = await self.underlying_tool.acall(*args, **kwargs)
        else:
            res = self.underlying_tool(*args, **kwargs)
        self._post_execute_sync(action)
        return res


def wrap_governed_tools(
    tools: List[Any],
    con: sqlite3.Connection,
    agent_id: str,
    repo_root: str | Path,
    log_path: Optional[str | Path] = None,
) -> List[Any]:
    """Wrap a list of OpenHands tools with ChronosToolGateway."""
    return [
        ChronosToolGateway(
            underlying_tool=t,
            con=con,
            agent_id=agent_id,
            repo_root=repo_root,
            log_path=log_path,
        )
        for t in tools
    ]
