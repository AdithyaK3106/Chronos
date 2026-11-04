"""`.chronos/protected.yml`: the documented `policies:` format, per-policy mode, loud rejection."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from chronos.core.db import get_connection
from chronos.integrations import claude_code as cc
from chronos.security.gates import (
    ProtectedConfigError,
    check_gate_approval,
    load_protected_modules,
    parse_protected_config,
    protected_config_error,
)

DOCUMENTED = """\
policies:
  - id: core-security
    paths:
      - "src/billing/**"
    mode: human_approval
    denial_reason: "Security-critical module requires human gate approval."
  - id: migrations
    paths: ["db/migrations/*.sql"]
    mode: deny
    denial_reason: "Migrations are written by the DBA team."
"""


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src" / "billing").mkdir(parents=True)
    (tmp_path / "src" / "billing" / "charge.py").write_text("x = 1\n")
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    (tmp_path / "db" / "migrations" / "001.sql").write_text("select 1;\n")
    (tmp_path / "app.py").write_text("y = 1\n")
    get_connection(tmp_path / ".chronos" / "chronos.db").close()
    return tmp_path


def _write(repo, text):
    (repo / ".chronos" / "protected.yml").write_text(text)


def _edit(repo, rel):
    payload = {
        "tool_name": "Edit", "cwd": str(repo), "session_id": "s", "tool_use_id": "e1",
        "tool_input": {"file_path": str(repo / rel), "old_string": "1", "new_string": "2"},
    }
    code, out, err = cc.run_hook("pre", json.dumps(payload))
    assert code == 0, err
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_documented_format_is_loaded(repo):
    _write(repo, DOCUMENTED)
    mods = {m["name"]: m for m in load_protected_modules(repo)}
    assert mods["core-security"]["mode"] == "ask"  # human_approval is an alias of ask
    assert mods["migrations"]["mode"] == "deny"
    ok, reason = check_gate_approval("src/billing/charge.py", repo)
    assert not ok and "core-security" in reason and "requires human gate approval." in reason


def test_policy_mode_and_reason_reach_the_hook(repo):
    _write(repo, DOCUMENTED)
    ask = _edit(repo, "src/billing/charge.py")
    assert ask["permissionDecision"] == "ask"
    assert "Security-critical module requires human gate approval." in ask["permissionDecisionReason"]
    deny = _edit(repo, "db/migrations/001.sql")
    assert deny["permissionDecision"] == "deny"
    assert "Migrations are written by the DBA team." in deny["permissionDecisionReason"]
    assert _edit(repo, "app.py") is None


def test_strict_preset_still_denies_an_ask_policy(repo):
    _write(repo, DOCUMENTED)
    (repo / ".chronos" / "config.json").write_text(json.dumps({"policy": {"preset": "strict"}}))
    assert _edit(repo, "src/billing/charge.py")["permissionDecision"] == "deny"


def test_legacy_protected_modules_still_work(repo):
    _write(repo, "protected_modules:\n  - name: billing\n    paths: ['src/billing/**']\n")
    assert _edit(repo, "src/billing/charge.py")["permissionDecision"] == "ask"


@pytest.mark.parametrize("text,needle", [
    ("policy:\n  - id: x\n    paths: ['a']\n", "unknown top-level key(s) policy"),
    ("policies:\n  - id: x\n    path: ['a']\n", "unknown key(s) path"),
    ("policies:\n  - id: x\n    paths: ['a']\n    deny_reason: no\n", "unknown key(s) deny_reason"),
    ("policies:\n  - id: x\n    paths: ['a']\n    mode: block\n", "mode 'block'"),
    ("policies:\n  - id: x\n    paths: []\n", "non-empty list"),
    ("policies:\n  - paths: ['a']\n", "needs a string `id`"),
    ("policies: {id: x}\n", "`policies` must be a list"),
])
def test_unknown_keys_and_bad_values_are_rejected(text, needle):
    import yaml

    with pytest.raises(ProtectedConfigError, match=None) as exc:
        parse_protected_config(yaml.safe_load(text))
    assert needle in str(exc.value)


def test_invalid_config_fails_closed_with_the_reason(repo):
    _write(repo, "policies:\n  - id: x\n    path: ['src/**']\n")
    assert "unknown key(s) path" in protected_config_error(repo)
    d = _edit(repo, "app.py")  # an ordinary file: protected because the config was refused
    assert d["permissionDecision"] == "ask"
    assert "protected.yml is invalid" in d["permissionDecisionReason"]


def test_doctor_reports_invalid_config(repo):
    _write(repo, "policies:\n  - id: x\n    paths: ['a']\n    mode: block\n")
    r = subprocess.run([sys.executable, "-m", "chronos.cli.main", "doctor"], cwd=repo, capture_output=True, text=True)
    assert "protected.yml   : INVALID" in r.stdout and "mode 'block'" in r.stdout
    assert r.returncode == 1


def test_valid_and_absent_configs_have_no_error(repo):
    assert protected_config_error(repo) is None
    _write(repo, DOCUMENTED)
    assert protected_config_error(repo) is None


# ----------------------------------------------------------------------------- item 9: docs are not protected
@pytest.mark.parametrize("rel,protected", [
    ("docs/oauth.md", False),
    ("src/authors/list.py", False),
    ("SECURITY.md", False),
    ("auth/README.md", False),
    ("docs/security/threat-model.rst", False),
    ("doc/api/auth.html", False),
    ("docs/policy.mdx", False),
    ("src/auth/login.py", True),
    ("src/oauth.py", True),
    ("auth/__init__.py", True),
    ("config/permissions.yaml", True),
    ("src/policies/rate.ts", True),
    ("docs/credentials.md", True),   # credentials are never exempt
    ("docs/.env", True),             # the .env rule is not word-based
])
def test_default_words_guard_code_not_docs(tmp_path, rel, protected):
    from chronos.security.gates import find_protected_module

    assert bool(find_protected_module(rel, tmp_path)) is protected


def test_custom_policy_can_still_protect_docs(repo):
    _write(repo, "policies:\n  - id: docs\n    paths: ['docs/**']\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "oauth.md").write_text("x\n")
    assert _edit(repo, "docs/oauth.md")["permissionDecision"] == "ask"
