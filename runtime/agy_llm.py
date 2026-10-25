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
        prompt_parts = [
            "You are a raw LLM completion endpoint for an external agent runtime.\n"
            "Output ONLY the single next assistant message in the conversation below.\n"
            "Do NOT attempt multi-turn execution or internal host tools. Generate text or a tool call and stop immediately."
        ]

        # Use recent context to avoid multi-turn prompt inflation
        recent_messages = messages[-6:] if len(messages) > 6 else messages
        for m in recent_messages:
            role = m.role.upper()
            text = "".join(getattr(c, "text", "") for c in m.content)
            # Condense verbose OpenHands system prompts down to essential role
            if role == "SYSTEM" and len(text) > 400:
                text = "You are an autonomous AI coding assistant. Solve the user's task accurately using available tools."
            prompt_parts.append(f"[{role}]: {text}")

        if tools:
            tool_descs = [f"- {t.name}: {getattr(t, 'description', '')[:180]}" for t in tools]
            prompt_parts.append("\nAVAILABLE TOOLS:\n" + "\n".join(tool_descs))
            prompt_parts.append(
                '\nTo call a tool, respond with:\n'
                'TOOL_CALL: {"name": "<tool_name>", "arguments": {<args>}}'
            )

        full_prompt = "\n\n".join(prompt_parts)

        cmd = [
            self.agy_binary,
            "--output-format", "json",
            "--disable-slash-commands",
            "--dangerously-skip-permissions",
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
                # If wrapped in markdown code fence, strip it
                if call_json_str.startswith("```"):
                    call_json_str = call_json_str.split("```")[1]
                    if call_json_str.startswith("json"):
                        call_json_str = call_json_str[4:]
                    call_json_str = call_json_str.strip()
                call_dict = json.loads(call_json_str)
                tool_calls.append(
                    MessageToolCall(
                        id=f"call_agy_{uuid.uuid4().hex[:6]}",
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

        prompt_tokens = usage_data.get("input_tokens", 0) if isinstance(usage_data, dict) else 0
        completion_tokens = usage_data.get("output_tokens", 0) if isinstance(usage_data, dict) else 0
        cache_read_tokens = usage_data.get("cache_read_tokens", 0) if isinstance(usage_data, dict) else 0
        thinking_tokens = usage_data.get("thinking_tokens", 0) if isinstance(usage_data, dict) else 0
        conv_id = data.get("conversation_id", "agy_conv") if isinstance(data, dict) else "agy_conv"

        if hasattr(self, "metrics") and self.metrics is not None:
            self.metrics.add_token_usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=0,
                context_window=1000000,
                response_id=conv_id,
                reasoning_tokens=thinking_tokens,
            )

        raw_response = ModelResponse(
            id=conv_id,
            choices=[Choices(message=LiteMessage(content=response_text, role="assistant"))],
            model=self.model,
            usage=Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
        )

        return LLMResponse(
            message=assistant_msg,
            metrics=self.metrics.get_snapshot() if hasattr(self, "metrics") and self.metrics else MetricsSnapshot(model_name=self.model),
            raw_response=raw_response,
        )
