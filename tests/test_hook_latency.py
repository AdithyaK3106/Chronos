"""The hook's hot path stays free of heavy imports, and its shortcuts agree with the full models."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from chronos.core.config import PolicyConfig
from chronos.core.policy import POLICY_DEFAULTS, POLICY_PRESETS, load_policy_fast


@pytest.mark.parametrize("policy", [
    {}, {"preset": "strict"}, {"preset": "relaxed"}, {"preset": "standard"},
    {"preset": "strict", "protected_modules": "ask"}, {"outside_repo_writes": "allow", "bash_backstop": False},
    {"max_context_chars": 1234, "on_hook_error": "deny"},
])
def test_fast_policy_matches_the_model(tmp_path, policy):
    (tmp_path / ".chronos").mkdir()
    (tmp_path / ".chronos" / "config.json").write_text(json.dumps({"policy": policy, "repo_path": "x"}))
    assert vars(load_policy_fast(tmp_path)) == PolicyConfig(**policy).model_dump()


@pytest.mark.parametrize("policy", [{"preset": "nope"}, {"protected_modules": "maybe"}, {"unknown": 1}, {"bash_backstop": "yes"}])
def test_fast_policy_defers_anything_unusual_to_the_model(tmp_path, policy):
    (tmp_path / ".chronos").mkdir()
    (tmp_path / ".chronos" / "config.json").write_text(json.dumps({"policy": policy}))
    assert load_policy_fast(tmp_path) is None


def test_defaults_agree():
    assert PolicyConfig().model_dump() == POLICY_DEFAULTS
    assert set(POLICY_PRESETS) == {"relaxed", "standard", "strict"}


def test_pre_hook_does_not_import_pydantic_or_yaml(tmp_path):
    """Each heavy import here cost 20-150 ms on every Claude Code tool call."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chronos").mkdir()
    (tmp_path / "auth.py").write_text("x = 1\n")
    code = (
        "import json, sys\n"
        "from chronos.integrations.claude_code import run_hook\n"
        f"root = {str(tmp_path)!r}\n"
        "for ev in ({'tool_name': 'Edit', 'tool_input': {'file_path': root + '/auth.py', 'old_string': '1', 'new_string': '2'}},\n"
        "           {'tool_name': 'Bash', 'tool_input': {'command': 'ls && cat .env'}},\n"
        "           {'tool_name': 'Read', 'tool_input': {'file_path': root + '/auth.py'}}):\n"
        "    ev['cwd'] = root\n"
        "    run_hook('pre', json.dumps(ev))\n"
        "print(json.dumps(sorted(m for m in ('pydantic', 'yaml', 'urllib.request') if m in sys.modules)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert json.loads(out.strip().splitlines()[-1]) == []


def test_protected_yml_is_still_honoured(tmp_path):
    from chronos.security.gates import find_protected_module

    (tmp_path / ".chronos").mkdir()
    (tmp_path / ".chronos" / "protected.yml").write_text("protected_modules:\n  - name: billing\n    paths: ['billing/*']\n")
    (tmp_path / "billing").mkdir()
    (tmp_path / "billing" / "charge.py").write_text("x = 1\n")
    assert find_protected_module("billing/charge.py", tmp_path) is not None
