"""Chronos runtime integration for OpenHands SDK.

Names are resolved lazily (PEP 562) so lightweight consumers, such as the per-tool-call
security hook, do not pay the multi-second cost of importing the OpenHands SDK.
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    "evaluate_tool_security": ".hooks",
    "run_hook_cli": ".hooks",
    "ChronosContextCondenser": ".condenser",
    "ChronosAgentRuntime": ".openhands_adapter",
    "AgyLLM": ".agy_llm",
}

__all__ = list(_LAZY)


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_name, __name__), name)
    globals()[name] = value
    return value
