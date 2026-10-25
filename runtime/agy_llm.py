"""OpenHands SDK LLM provider powered directly by the local Google Antigravity CLI (agy).

Allows running OpenHands autonomous coding agents without needing third-party API keys,
by leveraging your active authenticated AGY session (Gemini 3.8 Flash, Claude 4.6, GPT-OSS).
"""

import json
import subprocess
from typing import Any, Sequence
from litellm import Choices, Message as LiteMessage, ModelResponse, Usage
from pydantic import ConfigDict, Field

from openhands.sdk.llm import LLM, Message, MessageToolCall, TextContent
from openhands.sdk.llm.llm_response import LLMResponse
from openhands.sdk.llm.utils.metrics import MetricsSnapshot
from openhands.sdk.tool import ToolDefinition

class AgyLLM(LLM):
    """OpenHands LLM provider leveraging the local Google Antigravity CLI (agy)."""

    model: str = Field(default="gemini-3.8-flash-high")
    agy_binary: str = Field(default="agy")

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    def completion(
        self,
        messages: list[Message],
        tools: Sequence[ToolDefinition] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Run completion via local agy print mode."""
        prompt_parts = []
        for m in messages:
            role = m.role.upper()
            text = "".join(getattr(c, "text", "") for c in m.content)
            prompt_parts.append(f"[{role}]: {text}")

        if tools:
            tool_descs = [f"- {t.name}: {getattr(t, 'description', '')}" for t in tools]
            prompt_parts.append("\nAVAILABLE TOOLS:\n" + "\n".join(tool_descs))
            prompt_parts.append(
                '\nTo call a tool, respond with: TOOL_CALL: {"name": "<tool_name>", "arguments": {<args>}}'
            )

        full_prompt = "\n\n".join(prompt_parts)

        cmd = [
            self.agy_binary,
            "--output-format", "json",
            "--disable-slash-commands",
            f"--model={self.model}",
            "--print", full_prompt,
        ]

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            raise RuntimeError(f"agy CLI execution failed (exit {proc.returncode}): {proc.stderr}")

        try:
            data = json.loads(proc.stdout)
            response_text = data.get("response", "").strip()
            usage_data = data.get("usage", {})
        except Exception:
            response_text = proc.stdout.strip()
            usage_data = {}

        # Parse tool calls if generated
        tool_calls = []
        if "TOOL_CALL:" in response_text:
            try:
                call_json_str = response_text.split("TOOL_CALL:", 1)[1].strip()
                call_dict = json.loads(call_json_str)
                tool_calls.append(
                    MessageToolCall(
                        id="call_agy_1",
                        name=call_dict["name"],
                        arguments=json.dumps(call_dict.get("arguments", {})),
                        origin="completion",
                    )
                )
            except Exception:
                pass

        assistant_msg = Message(
            role="assistant",
            content=[TextContent(text=response_text)],
            tool_calls=tool_calls if tool_calls else None,
        )

        raw_response = ModelResponse(
            id=data.get("conversation_id", "agy_conv") if isinstance(data, dict) else "agy_conv",
            choices=[Choices(message=LiteMessage(content=response_text, role="assistant"))],
            model=self.model,
            usage=Usage(
                prompt_tokens=usage_data.get("input_tokens", 0) if isinstance(usage_data, dict) else 0,
                completion_tokens=usage_data.get("output_tokens", 0) if isinstance(usage_data, dict) else 0,
                total_tokens=usage_data.get("total_tokens", 0) if isinstance(usage_data, dict) else 0,
            ),
        )

        return LLMResponse(
            message=assistant_msg,
            metrics=MetricsSnapshot(model_name=self.model),
            raw_response=raw_response,
        )
