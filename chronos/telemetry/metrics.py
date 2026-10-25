"""Lightweight token, latency, and task outcome telemetry."""

import time
from typing import Any
from pydantic import BaseModel, Field

class TaskTelemetry(BaseModel):
    task_id: str
    agent_id: str
    start_time: float = Field(default_factory=time.time)
    end_time: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    context_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    tool_calls_attempted: int = 0
    tool_calls_allowed: int = 0
    tool_calls_denied: int = 0
    provenance_entries: list[dict[str, Any]] = Field(default_factory=list)

    def record_tool_call(self, tool_name: str, allowed: bool, reason: str = ""):
        self.tool_calls_attempted += 1
        if allowed:
            self.tool_calls_allowed += 1
        else:
            self.tool_calls_denied += 1
        self.provenance_entries.append({
            "tool_name": tool_name,
            "allowed": allowed,
            "reason": reason,
            "timestamp": time.time(),
        })

    def record_tokens(self, prompt: int, completion: int, context: int = 0):
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.context_tokens += context
        self.total_tokens = self.prompt_tokens + self.completion_tokens + self.context_tokens

    def finish(self) -> dict[str, Any]:
        self.end_time = time.time()
        duration_s = round(self.end_time - self.start_time, 3)
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "duration_seconds": duration_s,
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "context_tokens": self.context_tokens,
            "tool_calls": {
                "attempted": self.tool_calls_attempted,
                "allowed": self.tool_calls_allowed,
                "denied": self.tool_calls_denied,
            },
            "provenance_count": len(self.provenance_entries),
        }
