"""Policy presets: relaxed / standard / strict, with explicit settings taking precedence."""

import json

import pytest
from pydantic import ValidationError

from chronos.core.config import POLICY_PRESETS, ChronosConfig, PolicyConfig
from chronos.integrations import claude_code as cc


def test_no_preset_keeps_todays_defaults():
    assert PolicyConfig().model_dump(exclude={"preset"}) == POLICY_PRESETS["standard"]


@pytest.mark.parametrize("name", ["relaxed", "standard", "strict"])
def test_each_preset_sets_every_field(name):
    p = PolicyConfig(preset=name)
    assert p.model_dump(exclude={"preset"}) == POLICY_PRESETS[name]


def test_explicit_fields_override_the_preset():
    p = PolicyConfig(preset="strict", protected_modules="ask")
    assert p.protected_modules == "ask" and p.on_hook_error == "deny" and p.max_context_chars == 4000


def test_unknown_preset_is_rejected():
    with pytest.raises(ValidationError):
        PolicyConfig(preset="paranoid")


def test_preset_from_config_file_drives_the_hook(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".chronos").mkdir()
    (tmp_path / "auth.py").write_text("def login():\n    return 1\n")
    (tmp_path / ".chronos" / "config.json").write_text(json.dumps({"policy": {"preset": "strict"}}))
    assert ChronosConfig.load(tmp_path).policy.protected_modules == "deny"

    payload = {"tool_name": "Edit", "cwd": str(tmp_path),
               "tool_input": {"file_path": str(tmp_path / "auth.py"), "old_string": "1", "new_string": "2"}}
    code, out, _ = cc.run_hook("pre", json.dumps(payload))
    assert code == 0 and json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"

    (tmp_path / ".chronos" / "config.json").write_text(json.dumps({"policy": {"preset": "relaxed"}}))
    code, out, _ = cc.run_hook("pre", json.dumps(payload))
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
