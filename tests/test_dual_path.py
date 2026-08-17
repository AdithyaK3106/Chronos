"""Dual-path rule distribution: git-native default, Packmind opt-in.

Run: pytest tests/test_dual_path.py -v

No network, no Docker, no gh. Git calls are mocked except where a real
temp repo is cheaper than a mock.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import rule_store, rule_submission  # noqa: E402
from chronos.curator import _get_submission_path  # noqa: E402
from chronos.playbook import Packmind, PackmindNotConfigured  # noqa: E402

CANDIDATE = {
    "rule_text": "IF a function makes an HTTP request THEN it must use the shared client.",
    "confidence": 0.95,
    "language": "typescript",
    "evidence_node": "src/api/actors.ts::getActor",
    "evidence_valid_at": "2026-07-20T15:50:43+00:00",
    "evidence_commit_context": "33 facts, 0 superseded",
    "agent_id": "claude-code",
}


@pytest.fixture
def no_packmind(monkeypatch):
    monkeypatch.delenv("PACKMIND_API_URL", raising=False)
    monkeypatch.delenv("PACKMIND_API_KEY", raising=False)


def test_git_native_selected_when_unset(no_packmind):
    assert _get_submission_path() == "git-native"


def test_packmind_selected_when_set(monkeypatch):
    monkeypatch.setenv("PACKMIND_API_URL", "http://fake")
    assert _get_submission_path() == "packmind"


def test_submit_git_native_writes_rule_file(tmp_path, monkeypatch, no_packmind):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(rule_submission.shutil, "which", lambda _: None)  # no gh

    calls = []

    def fake_run(args, cwd=None, **kw):
        calls.append(args)
        # rev-parse must return a branch name or the code assumes git is absent
        out = "main\n" if "rev-parse" in args else ""
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(rule_submission.subprocess, "run", fake_run)
    # Keep the proposal out of the developer's real chronos.db.
    monkeypatch.setattr(rule_store, "propose_rule", lambda *a, **k: None)

    r = rule_submission.submit_git_native(CANDIDATE, str(tmp_path))

    rule_id = r["rule_id"]
    written = tmp_path / ".chronos" / "rules" / f"{rule_id}.yml"
    assert written.exists(), f"rule file not written: {written}"
    assert r["status"] == "proposed"
    assert r["pr_url"] is None, "no gh CLI -> no PR"
    assert r["branch"] == f"chronos/rule-{rule_id}"
    assert any("checkout" in a for a in calls), "should have created a branch"


def test_submit_git_native_never_overwrites_generator_output(
        tmp_path, monkeypatch, no_packmind):
    """rule_generator.py owns the pattern; submission must not clobber it."""
    (tmp_path / ".git").mkdir()
    rule_id = rule_submission.rule_id_for(CANDIDATE)
    rules = tmp_path / ".chronos" / "rules"
    rules.mkdir(parents=True)
    original = "id: generated-by-wedge4\nrule:\n  pattern: fetch($$$)\n"
    (rules / f"{rule_id}.yml").write_text(original, encoding="utf-8")

    monkeypatch.setattr(rule_submission.shutil, "which", lambda _: None)
    monkeypatch.setattr(rule_submission.subprocess, "run",
                        lambda a, cwd=None, **k: subprocess.CompletedProcess(
                            a, 0, stdout="main\n", stderr=""))
    monkeypatch.setattr(rule_store, "propose_rule", lambda *a, **k: None)

    rule_submission.submit_git_native(CANDIDATE, str(tmp_path))
    assert (rules / f"{rule_id}.yml").read_text(encoding="utf-8") == original


def test_approve_rule_advances_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONOS_SQLITE", str(tmp_path / "test.db"))
    con = rule_store.connect(str(tmp_path / "test.db"))
    try:
        rule_store.propose_rule("r-001", "typescript", "IF x THEN y",
                                "id: r-001\n", con=con)
        con.commit()
        assert rule_store.get_rule("r-001", con=con)["status"] == rule_store.PROPOSED

        r = rule_store.approve_rule("r-001", con=con)
        con.commit()
        assert r["approved"] is True
        assert rule_store.get_rule("r-001", con=con)["status"] == rule_store.UNVALIDATED
    finally:
        con.close()


def test_proposed_rules_are_not_enforced(tmp_path, monkeypatch):
    """The safety property: an unapproved rule must never reach the enforcer."""
    monkeypatch.setenv("CHRONOS_SQLITE", str(tmp_path / "test.db"))
    con = rule_store.connect(str(tmp_path / "test.db"))
    try:
        rule_store.propose_rule("r-002", "typescript", "IF x THEN y",
                                "id: r-002\n", con=con)
        con.commit()
        active = rule_store.get_active_rules(con=con)
        assert not any(r["rule_id"] == "r-002" for r in active), \
            "proposed rule leaked into enforcement"

        rule_store.approve_rule("r-002", con=con)
        con.commit()
        active = rule_store.get_active_rules(con=con)
        assert any(r["rule_id"] == "r-002" for r in active), \
            "approved rule should now be enforced (warn-only)"
    finally:
        con.close()


def test_proposed_cannot_jump_to_blocking(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONOS_SQLITE", str(tmp_path / "test.db"))
    con = rule_store.connect(str(tmp_path / "test.db"))
    try:
        rule_store.propose_rule("r-003", "typescript", "IF x THEN y", None, con=con)
        con.commit()
        r = rule_store.promote_to_blocking("r-003", "someone", con=con)
        assert r["promoted"] is False
        assert "approve-rule" in r["reason"]
    finally:
        con.close()


def test_packmind_not_configured_raised(no_packmind):
    with pytest.raises(PackmindNotConfigured):
        Packmind()


def test_packmind_not_configured_is_a_packmind_error():
    """Existing `except PackmindError` handlers must keep working."""
    from chronos.playbook import PackmindError
    assert issubclass(PackmindNotConfigured, PackmindError)


def test_rule_id_is_stable(no_packmind):
    """Same lesson twice -> same id, so re-proposal reuses the branch."""
    a = rule_submission.rule_id_for(CANDIDATE)
    b = rule_submission.rule_id_for(dict(CANDIDATE))
    assert a == b
    assert rule_submission.rule_id_for({"rule_text": "different"}) != a


def _git(args, cwd):
    return subprocess.run(["git", "-C", str(cwd)] + args,
                          capture_output=True, text=True, check=False)


@pytest.mark.skipif(not rule_submission.shutil.which("git"), reason="git not installed")
def test_rule_file_commits_even_though_chronos_is_gitignored(
        tmp_path, monkeypatch, no_packmind):
    """The rule file must reach a commit in a repo that gitignores .chronos/.

    Regression for the bug that made the git-native PR path unreachable in
    every repo set up the way we recommend: `git add` (no -f) exits 1 on an
    ignored path, `_commit` returns False, and `_open_pr` is never called, so
    the run degrades to `pr_url: None` while *looking* exactly like the tested
    "no gh installed" case.

    Uses a real git repo on purpose. The other tests here fake `.git` with
    mkdir, which is why none of them could catch this -- with no real git,
    `git add` never runs and the ignore rule never applies.
    """
    monkeypatch.setenv("CHRONOS_SQLITE", str(tmp_path / "test.db"))
    assert _git(["init", "-q", "-b", "main"], tmp_path).returncode == 0 or \
        _git(["init", "-q"], tmp_path).returncode == 0
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    # The setup Chronos itself recommends, and the source of the bug.
    (tmp_path / ".gitignore").write_text(".chronos/\n", encoding="utf-8")
    _git(["add", ".gitignore"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)

    # No remote and no gh: _open_pr must still be *reached* and fail at push,
    # which is the documented degradation. What must not happen is failing
    # earlier, at the commit.
    r = rule_submission.submit_git_native(CANDIDATE, str(tmp_path))
    rule_id = r["rule_id"]

    branch = f"{rule_submission.BRANCH_PREFIX}{rule_id}"
    assert r["branch"] == branch, "branch was not created"

    # The commit exists on the rule branch and contains the rule file.
    files = _git(["show", "--name-only", "--format=", branch], tmp_path).stdout
    assert f"{rule_id}.yml" in files, (
        f"rule file never made it into the commit on {branch}; got: {files!r}")

    # And the developer was put back where they started.
    head = _git(["rev-parse", "--abbrev-ref", "HEAD"], tmp_path).stdout.strip()
    assert head != branch, "left the developer on the chronos branch"


def test_resolve_repo_path_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONOS_REPO_PATH", str(tmp_path / "from-env"))
    assert rule_submission.resolve_repo_path(None) == str(tmp_path / "from-env")
    # explicit wins over env
    assert rule_submission.resolve_repo_path(str(tmp_path / "x")) == str(tmp_path / "x")

    monkeypatch.delenv("CHRONOS_REPO_PATH")
    dot = tmp_path / ".chronos"
    dot.mkdir()
    (dot / "config.json").write_text('{"repo": "/from/config"}', encoding="utf-8")
    monkeypatch.setenv("CHRONOS_SQLITE", str(dot / "chronos.db"))
    assert rule_submission.resolve_repo_path(None) == "/from/config"

    monkeypatch.delenv("CHRONOS_SQLITE")
    assert rule_submission.resolve_repo_path(None) == os.getcwd()
