"""Findings from the live Claude Code run: multi-line commands and dependency manifests."""

from __future__ import annotations

import json
import subprocess

import pytest

from chronos.core.db import get_connection
from chronos.core.paths import audit_log_path
from chronos.integrations import claude_code as cc
from chronos.security.file_rules import evaluate_edit
from chronos.security.shell import analyze

HEREDOC_END = "E" + "OF"  # kept out of the literal so no shell heredoc can end on this line
MULTILINE = "\n".join([
    f"cat >> app.py <<'{HEREDOC_END}'",
    "def retry():",
    "    return 1",
    HEREDOC_END,
    "printf 'requests\\ntenacity\\n' > requirements.txt",
    "sed -i 's/pip install pytest/pip install -r requirements.txt pytest/' .github/workflows/ci.yml",
    "pip install -q -r requirements.txt pytest 2>&1 | tail -2; python -m pytest -q",
])


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("x\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    get_connection(tmp_path / ".chronos" / "chronos.db").close()
    return tmp_path


def _pre(repo, command):
    out = cc.run_hook("pre", json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}))[1]
    return json.loads(out)["hookSpecificOutput"] if out else None


def test_every_line_of_a_multiline_command_is_checked(repo):
    a = analyze(MULTILINE, repo)
    assert [c.prog for c in a.commands][:4] == ["cat", "printf", "sed", "pip"]
    assert str(repo / ".github" / "workflows" / "ci.yml") in a.writes
    d = _pre(repo, MULTILINE)
    assert d and d["permissionDecision"] == "ask"
    rules = {(json.loads(line).get("metadata") or {}).get("rule") for line in audit_log_path(repo).read_text().splitlines()}
    assert {"integrity.ci_config", "dependencies.add", "dependencies.manifest"} <= rules


@pytest.mark.parametrize("cmd,words", [
    ("echo 'a\nb' > x.txt", [["echo", "a\nb"]]),
    ('git commit -m "line1\nline2"', [["git", "commit", "-m", "line1\nline2"]]),
    ("ls \\\n  -la", [["ls", "-la"]]),
    ("ls # cat .env\necho ok", [["ls"], ["echo", "ok"]]),
    ("cd app\nrm -rf build", [["cd", "app"], ["rm", "-rf", "build"]]),
])
def test_newlines_split_commands_except_inside_quotes(repo, cmd, words):
    assert [c.words for c in analyze(cmd, repo).commands] == words


def test_reason_has_a_single_chronos_prefix(repo):
    d = _pre(repo, "git push --force origin main\ngit reset --hard")
    assert d["permissionDecisionReason"].startswith("[Chronos] ") and d["permissionDecisionReason"].count("[Chronos]") == 1


@pytest.mark.parametrize("rel,old,new,added", [
    ("requirements.txt", "requests\n", "requests\ntenacity>=8\n", "tenacity"),
    ("package.json", '{\n  "dependencies": {\n    "react": "^18.0.0"\n  }\n}', '{\n  "dependencies": {\n    "react": "^18.0.0",\n    "lodahs": "^4"\n  }\n}', "lodahs"),
    ("pyproject.toml", '[project]\ndependencies = [\n  "httpx",\n]\n', '[project]\ndependencies = [\n  "httpx",\n  "colourama",\n]\n', "colourama"),
    ("go.mod", "require (\n\tgithub.com/a/b v1.0.0\n)\n", "require (\n\tgithub.com/a/b v1.0.0\n\tgithub.com/evil/x v0.1.0\n)\n", "github.com/evil/x"),
    ("Cargo.toml", "[dependencies]\nserde = '1'\n", "[dependencies]\nserde = '1'\ntokio = { version = '1' }\n", "tokio"),
])
def test_manifest_edit_adding_a_dependency_asks(rel, old, new, added):
    hits = [h for h in evaluate_edit(rel, old, new) if h.rule == "dependencies.manifest"]
    assert hits and hits[0].decision == "ask" and added in hits[0].reason


@pytest.mark.parametrize("rel,old,new", [
    ("requirements.txt", "requests==2.0\n", "requests==2.1\n"),
    ("package.json", '{\n  "version": "1.0.0"\n}', '{\n  "version": "1.0.1"\n}'),
    ("pyproject.toml", '[project]\nname = "x"\n', '[project]\nname = "x"\ndescription = "y"\n'),
])
def test_manifest_edit_without_a_new_dependency_passes(rel, old, new):
    assert not [h for h in evaluate_edit(rel, old, new) if h.rule == "dependencies.manifest"]
