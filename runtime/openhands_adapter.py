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
from runtime.hooks import evaluate_tool_security

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

    def create_agent(
        self,
        llm: LLM,
        agent_id: str = "default_agent",
        tools: list[Tool] | None = None,
        condenser: ChronosContextCondenser | None = None,
    ) -> Agent:
        """Create an OpenHands Agent instance configured with tools and condenser."""
        configured_tools = tools if tools is not None else [Tool(name=FileEditorTool.name)]
        agent_condenser = condenser or ChronosContextCondenser(max_events=20, keep_first=2)

        return Agent(
            llm=llm,
            tools=configured_tools,
            condenser=agent_condenser,
        )

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
    ) -> tuple[Conversation, TaskTelemetry]:
        """Execute a task through an OpenHands Conversation with Chronos telemetry."""
        target_ws = str(Path(workspace_dir or self.repo_root).resolve())
        telemetry = TaskTelemetry(task_id="task_001", agent_id=agent_id)

        conversation_events: list[Event] = []

        def event_callback(event: Event):
            conversation_events.append(event)

        conversation = Conversation(
            agent=agent,
            callbacks=[event_callback],
            workspace=target_ws,
        )

        # Submit initial user prompt
        conversation.send_message(task_prompt)
        conversation.run()

        telemetry.finish()
        return conversation, telemetry
