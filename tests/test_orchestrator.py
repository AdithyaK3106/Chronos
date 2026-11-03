"""`chronos run --engine claude` loop, driven by a scripted fake engine (no model calls)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from chronos.core.db import get_connection
from chronos.core.repo import inspect_repository
from chronos.orchestrator import ClaudeEngine, FakeEngine, run_task
from chronos.orchestrator.engines import EngineUnavailable, default_runner
from chronos.telemetry.runs import get_run

PY = sys.executable


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / ".git").mkdir()
    (r / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (r / "test_calc.py").write_text("from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    return r


TEST_CMD = f'"{PY}" -m pytest -q -p no:cacheprovider test_calc.py'


def fix(root: Path) -> None:
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")


def wrong(root: Path) -> None:
    (root / "calc.py").write_text("def add(a, b):\n    return a * b\n")


def _memories(repo):
    con = get_connection(repo / ".chronos" / "chronos.db")
    try:
        return [dict(r) for r in con.execute("SELECT content, outcome, source, task_id FROM memories")]
    finally:
        con.close()


def test_failing_round_is_fed_back_then_tests_pass(repo):
    engine = FakeEngine([wrong, fix])
    out = run_task(repo, "Fix add() in calc.py", engine, TEST_CMD, max_rounds=3, sandbox=False, use_context=False)

    assert out.success and len(out.rounds) == 2
    assert [r.tests_passed for r in out.rounds] == [False, True]
    assert engine.session_ids == [None, "fake-session"]           # round 2 resumes the same session
    assert "tests still fail" in engine.prompts[1] and "assert" in engine.prompts[1]
    assert TEST_CMD in engine.prompts[0]

    con = get_connection(repo / ".chronos" / "chronos.db")
    run = get_run(con, out.run_id)
    con.close()
    assert run["status"] == "SUCCESS" and run["arm"] == "chronos_run"
    assert [e["event_type"] for e in run["events"]].count("round_completed") == 2

    (mem,) = _memories(repo)
    assert mem["outcome"] == "success" and mem["source"] == "chronos_run" and mem["task_id"] == out.run_id


def test_gives_up_after_max_rounds(repo):
    out = run_task(repo, "Fix add()", FakeEngine([wrong, wrong, wrong, fix]), TEST_CMD, max_rounds=2,
                   sandbox=False, use_context=False)
    assert not out.success and len(out.rounds) == 2
    (mem,) = _memories(repo)
    assert mem["outcome"] == "failure" and "still fails" in mem["content"]


def test_agent_claiming_success_does_not_count(repo):
    out = run_task(repo, "Fix add()", FakeEngine([]), TEST_CMD, max_rounds=1, sandbox=False, use_context=False)
    assert not out.success


def test_context_is_resolved_into_the_first_prompt(repo):
    engine = FakeEngine([fix])
    out = run_task(repo, "Fix the add function in calc.py so it adds", engine, TEST_CMD, max_rounds=1, sandbox=False)
    assert out.success
    if out.context_chars:
        assert "<chronos-context>" in engine.prompts[0]


def test_claude_engine_command_line():
    eng = ClaudeEngine(model="haiku", binary="/usr/bin/claude")
    argv = eng.argv("do it")
    assert argv[:3] == ["/usr/bin/claude", "-p", "do it"]
    assert argv[argv.index("--output-format") + 1] == "json" and "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    resumed = eng.argv("again", session_id="abc")
    assert resumed[resumed.index("--resume") + 1] == "abc"
    assert "--allowedTools" not in argv and "--allowedTools" in ClaudeEngine(binary="x", allow_bash=True).argv("p")


def test_claude_engine_parses_results():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "Done",
                     "session_id": "s-1", "total_cost_usd": 0.0123, "num_turns": 4,
                     "usage": {"input_tokens": 1200, "output_tokens": 300}})
    r = ClaudeEngine.parse(0, "some log line\n" + ok, "")
    assert r.ok and r.session_id == "s-1" and r.cost_usd == 0.0123 and r.turns == 4 and r.input_tokens == 1200

    err = json.dumps({"is_error": True, "result": "Credit balance too low", "session_id": "s-2"})
    r = ClaudeEngine.parse(1, err, "")
    assert not r.ok and r.session_id == "s-2" and "Credit" in r.error

    r = ClaudeEngine.parse(2, "not json", "boom: auth failed")
    assert not r.ok and r.session_id is None and "auth failed" in r.error


def test_missing_claude_binary_is_a_clear_error(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(EngineUnavailable):
        ClaudeEngine().argv("x")


def _bwrap_works() -> bool:
    try:
        from chronos.sandbox.runner import _require_bwrap

        _require_bwrap()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap sandbox not available here")
def test_commands_run_inside_the_sandbox(repo):
    (repo / ".env").write_text("API_KEY=super-secret-value\n")
    out = run_task(repo, "Fix add()", FakeEngine([fix]), "cat .env; echo SANDBOX=$CHRONOS_SANDBOX; " + TEST_CMD,
                   max_rounds=1, sandbox=True, use_context=False)
    assert out.success and out.sandboxed
    assert "super-secret-value" not in out.final_test_output
    assert "SANDBOX=1" in out.final_test_output


FAKE_CLAUDE = r'''#!{py}
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[0] == "-p" and "--output-format" in args
resumed = "--resume" in args
body = "a + b" if resumed else "a * b"
Path("calc.py").write_text(f"def add(a, b):\n    return {{body}}\n".replace("{{body}}", body))
Path("claude_calls.log").open("a").write(" ".join(args[2:]) + "\n")
print(json.dumps({{"type": "result", "is_error": False, "result": "edited", "session_id": "sess-42",
                  "total_cost_usd": 0.001, "num_turns": 2, "usage": {{"input_tokens": 10, "output_tokens": 5}}}}))
'''


@pytest.mark.skipif(sys.platform == "win32", reason="the fake claude is a shebang script")
def test_cli_end_to_end_with_a_fake_claude_binary(repo, tmp_path, monkeypatch):
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE.format(py=PY))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{__import__('os').pathsep}{__import__('os').environ['PATH']}")

    r = subprocess.run(
        [PY, "-m", "chronos.cli.main", "run", "Fix add() in calc.py", "--engine", "claude", "--repo", str(repo),
         "--test", TEST_CMD, "--max-rounds", "3", "--no-sandbox"],
        capture_output=True, text=True, timeout=300, cwd=repo,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "round 1: tests fail" in r.stdout and "round 2: tests pass" in r.stdout
    assert "Done in 2 round(s)" in r.stdout and "$0.0020" in r.stdout
    calls = (repo / "claude_calls.log").read_text().splitlines()
    assert len(calls) == 2 and "--resume sess-42" in calls[1]


def test_cli_requires_a_test_command(repo):
    import subprocess

    r = subprocess.run([PY, "-m", "chronos.cli.main", "run", "x", "--engine", "claude", "--repo", str(repo)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 2 and "--test" in r.stderr
