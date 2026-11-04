"""Policy presets and defaults, and a pydantic-free reader for the hook's hot path.

`PolicyConfig` (chronos.core.config) is the validated model. Importing pydantic costs ~150 ms,
which every Claude Code hook call would pay, so the hook reads the policy with `load_policy_fast`
and falls back to the model only when the file holds something it does not understand.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

# Named policy presets. A preset only fills in fields the config does not set explicitly.
POLICY_PRESETS: dict[str, dict[str, object]] = {
    # Fewer interruptions: protected edits still ask, but a broken hook fails open.
    "relaxed": {"protected_modules": "ask", "on_hook_error": "allow", "outside_repo_writes": "ask", "bash_backstop": True, "max_context_chars": 8000},
    # The defaults: ask for protected edits, ask when the hook itself fails.
    "standard": {"protected_modules": "ask", "on_hook_error": "ask", "outside_repo_writes": "ask", "bash_backstop": True, "max_context_chars": 6000},
    # Fail closed: protected edits need `chronos gate grant`, hook failures deny, smaller context.
    "strict": {"protected_modules": "deny", "on_hook_error": "deny", "outside_repo_writes": "deny", "bash_backstop": True, "max_context_chars": 4000},
}

POLICY_DEFAULTS: dict[str, object] = {"preset": None, **POLICY_PRESETS["standard"]}

_CHOICES = {
    "protected_modules": {"ask", "deny"},
    "on_hook_error": {"ask", "deny", "allow"},
    "outside_repo_writes": {"ask", "deny", "allow"},
}


def load_policy_fast(root: str | Path | None) -> SimpleNamespace | None:
    """The repo's policy as a plain object, or None if the config needs full validation."""
    raw: object = {}
    if root is not None:
        cfg = Path(root) / ".chronos" / "config.json"
        if cfg.is_file():
            try:
                loaded = json.loads(cfg.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = {}  # same as ChronosConfig.load: an unreadable file means defaults
            raw = loaded.get("policy", {}) if isinstance(loaded, dict) else {}
    if not isinstance(raw, dict):
        return None
    if set(raw) - set(POLICY_DEFAULTS):
        return None
    preset = raw.get("preset")
    if preset is not None and preset not in POLICY_PRESETS:
        return None
    merged = {**POLICY_DEFAULTS, **(POLICY_PRESETS.get(preset, {}) if preset else {}), **raw}
    for key, allowed in _CHOICES.items():
        if merged[key] not in allowed:
            return None
    if type(merged["bash_backstop"]) is not bool or type(merged["max_context_chars"]) is not int:
        return None
    return SimpleNamespace(**merged)
