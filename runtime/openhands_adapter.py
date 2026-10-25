"""OpenHands Agent SDK integration and lifecycle harness for Chronos."""

import sqlite3
from pathlib import Path
from typing import Any

from openhands.sdk import (
    Agent,
    Conversation,
    Event,
    LLM,
    Tool,
)
from openhands.sdk.hooks.config import HookConfig, HookDefinition, HookMatcher, HookType
from openhands.sdk.hooks.types import HookEventType
from openhands.tools.file_editor import FileEditorTool

from chronos.audit.logger import append_audit_event
from chronos.context.graph import GraphStore, create_graph_store
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection
from chronos.telemetry.metrics import TaskTelemetry
from runtime.condenser import ChronosContextCondenser
from runtime.gateway import wrap_governed_tools
from runtime.hooks import evaluate_tool_security
from runtime.session import ChronosSessionBootstrap

class GovernedExecutorWrapper:
    """Delegating executor wrapper that enforces Chronos security and forwards close()."""

    def __init__(
        self,
        t_name: str,
        o_exec: Any,
        con: sqlite3.Connection,
        agent_id: str,
        repo_root: Path,
        log_path: Path | None,
    ):
        self.t_name = t_name
        self.o_exec = o_exec
        self.con = con
        self.agent_id = agent_id
        self.repo_root = repo_root
        self.log_path = log_path

    def __call__(self, action: Any, conversation: Any = None) -> Any:
        tool_input = (
            action.model_dump()
            if hasattr(action, "model_dump")
            else (action if isinstance(action, dict) else {})
        )
        allowed, reason = evaluate_tool_security(
            con=self.con,
            agent_id=self.agent_id,
            tool_name=self.t_name,
            tool_input=tool_input,
            repo_root=self.repo_root,
            log_path=self.log_path,
        )
        if not allowed:
            raise ValueError(f"[Chronos Veto] Action blocked by Chronos policy: {reason}")
        if self.o_exec is not None:
            result = self.o_exec(action, conversation)
            # P2: Incremental Bi-Temporal Graph Synchronization post-edit
            try:
                if self.t_name in ("file_editor", "edit_file", "write_file"):
                    from runtime.hooks import _extract_target_path
                    target = _extract_target_path(self.t_name, tool_input)
                    if target and (target.endswith(".py") or ".py" in target):
                        full_path = (self.repo_root / target).resolve()
                        if full_path.is_file():
                            from chronos.context.graph import SQLiteGraphStore
                            store = SQLiteGraphStore(self.con)
                            store.update_file(full_path)
            except Exception:
                pass
            return result
        raise NotImplementedError(f"Tool '{self.t_name}' has no executor")

    def close(self) -> None:
        if hasattr(self.o_exec, "close") and callable(self.o_exec.close):
            self.o_exec.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.o_exec, name)

class ChronosAgentRuntime:
    """Manages OpenHands Agent execution with deterministic Chronos governance."""

    def __init__(
        self,
        config: ChronosConfig | None = None,
        db_con: sqlite3.Connection | None = None,
        graph_store: GraphStore | None = None,
    ):
        self.config = config or ChronosConfig()
        self.repo_root = Path(self.config.repo_path).resolve()
        self.con = db_con or get_connection(self.repo_root / self.config.db_path)
        self.graph_store = graph_store or create_graph_store(self.con, backend=self.config.graph_backend)
        self.audit_log_path = self.repo_root / self.config.audit_log_path
        self.bootstrap = ChronosSessionBootstrap(repo_path=self.repo_root, con=self.con)

    def create_agent(
        self,
        llm: LLM,
        agent_id: str = "default_agent",
        tools: list[Tool] | None = None,
        condenser: ChronosContextCondenser | None = None,
        system_prompt: str | None = None,
        wrap_tools: bool = True,
    ) -> Agent:
        """Create an OpenHands Agent instance configured with governed tools and condenser."""
        configured_tools = tools if tools is not None else [Tool(name=FileEditorTool.name)]
        agent_condenser = condenser or ChronosContextCondenser(max_events=20, keep_first=2)

        kwargs: dict[str, Any] = {
            "llm": llm,
            "tools": configured_tools,
            "condenser": agent_condenser,
        }
        if system_prompt:
            kwargs["system_prompt"] = system_prompt

        agent = Agent(**kwargs)

        if wrap_tools:
            orig_init = agent._initialize

            def governed_init(state: Any):
                orig_init(state)
                for tool_name, tool_def in list(agent._tools.items()):
                    orig_exec = tool_def.executor
                    wrapper = GovernedExecutorWrapper(
                        t_name=tool_name,
                        o_exec=orig_exec,
                        con=self.con,
                        agent_id=agent_id,
                        repo_root=self.repo_root,
                        log_path=self.audit_log_path,
                    )
                    object.__setattr__(tool_def, "executor", wrapper)

            agent._initialize = governed_init

        return agent

    def intercept_and_evaluate_action(
        self,
        agent_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None,
    ) -> tuple[bool, str]:
        """Direct programmatic interception method before tool execution."""
        return evaluate_tool_security(
            con=self.con,
            agent_id=agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
            repo_root=self.repo_root,
            log_path=self.audit_log_path,
        )

    def run_task(
        self,
        agent: Agent,
        agent_id: str,
        task_prompt: str,
        workspace_dir: str | Path | None = None,
        target_files: list[str] | None = None,
    ) -> tuple[Conversation, TaskTelemetry]:
        """Execute a task through an OpenHands Conversation with Chronos telemetry."""
        target_ws = str(Path(workspace_dir or self.repo_root).resolve())
        telemetry = TaskTelemetry(task_id="task_001", agent_id=agent_id)

        conversation_events: list[Event] = []

        def event_callback(event: Event):
            conversation_events.append(event)

        # Automatic pre-Turn-1 zero-path knowledge bootstrap & injection
        bootstrap_res = self.bootstrap.bootstrap_session(
            task_prompt=task_prompt,
            target_files=target_files,
            agent_id=agent_id,
        )
        augmented_prompt = bootstrap_res["augmented_prompt"]

        conversation = Conversation(
            agent=agent,
            callbacks=[event_callback],
            workspace=target_ws,
        )

        # Submit initial user prompt with injected zero-path context envelope
        conversation.send_message(augmented_prompt)
        conversation.run()

        telemetry.finish()
        return conversation, telemetry
