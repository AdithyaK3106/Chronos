"""Coding-agent engines that the orchestrator can drive.

An engine starts a session on a prompt and can resume that same session with feedback (failing
test output), so the agent keeps its context between rounds. Engines only build and parse; the
orchestrator decides how a command runs (for example inside the `chronos exec` sandbox).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

# Runs a command in the repository and returns (exit code, stdout, stderr). The orchestrator
# provides one that applies the sandbox; tests can provide their own. A str command is a shell
# command line (run by the platform shell); a list is an argv. `env` adds environment variables.
CommandRunner = Callable[..., "tuple[int, str, str]"]


@dataclass
class EngineResult:
    session_id: Optional[str]
    ok: bool
    text: str = ""
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    turns: Optional[int] = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)


class Engine(Protocol):
    name: str

    def start(self, prompt: str, cwd: Path, run: CommandRunner) -> EngineResult: ...

    def resume(self, session_id: Optional[str], prompt: str, cwd: Path, run: CommandRunner) -> EngineResult: ...


class EngineUnavailable(RuntimeError):
    """The engine's CLI is not installed or not usable."""


class ClaudeEngine:
    """Claude Code in headless mode: `claude -p <prompt> --output-format json [--resume <id>]`."""

    name = "claude"

    def __init__(
        self,
        model: Optional[str] = None,
        permission_mode: str = "acceptEdits",
        allow_bash: bool = False,
        timeout_s: int = 900,
        binary: Optional[str] = None,
    ) -> None:
        self.model = model
        self.permission_mode = permission_mode
        self.allow_bash = allow_bash
        self.timeout_s = timeout_s
        self.binary = binary or shutil.which("claude")

    def argv(self, prompt: str, session_id: Optional[str] = None) -> list[str]:
        if not self.binary:
            raise EngineUnavailable("Claude Code is not installed (`claude` not on PATH). See https://claude.com/claude-code")
        argv = [self.binary, "-p", prompt, "--output-format", "json", "--permission-mode", self.permission_mode]
        if session_id:
            argv += ["--resume", session_id]
        if self.model:
            argv += ["--model", self.model]
        if self.allow_bash:
            argv += ["--allowedTools", "Bash"]
        return argv

    @staticmethod
    def parse(code: int, stdout: str, stderr: str) -> EngineResult:
        """Parse `--output-format json` output (one JSON object, possibly after other lines)."""
        data: dict = {}
        for line in reversed([ln for ln in stdout.strip().splitlines() if ln.strip()]):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if not data:
            try:
                data = json.loads(stdout)
            except (json.JSONDecodeError, ValueError):
                data = {}
        usage = data.get("usage") or {}
        is_error = bool(data.get("is_error")) or code != 0 or not data
        error = None
        if is_error:
            error = (data.get("result") if data else None) or stderr.strip()[-500:] or f"claude exited with {code}"
        return EngineResult(
            session_id=data.get("session_id"),
            ok=not is_error,
            text=str(data.get("result") or ""),
            cost_usd=data.get("total_cost_usd"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            turns=data.get("num_turns"),
            error=error,
            raw=data,
        )

    def start(self, prompt: str, cwd: Path, run: CommandRunner) -> EngineResult:
        return self.parse(*run(self.argv(prompt), cwd, self.timeout_s))

    def resume(self, session_id: Optional[str], prompt: str, cwd: Path, run: CommandRunner) -> EngineResult:
        return self.parse(*run(self.argv(prompt, session_id), cwd, self.timeout_s))


class FakeEngine:
    """Scripted engine for tests: each round applies the next edit function to the repository."""

    name = "fake"

    def __init__(self, rounds: list[Callable[[Path], None]]) -> None:
        self.rounds = list(rounds)
        self.prompts: list[str] = []
        self.session_ids: list[Optional[str]] = []

    def _step(self, prompt: str, cwd: Path, session_id: Optional[str]) -> EngineResult:
        self.prompts.append(prompt)
        self.session_ids.append(session_id)
        if not self.rounds:
            return EngineResult(session_id="fake-session", ok=True, text="(no change)")
        self.rounds.pop(0)(cwd)
        return EngineResult(session_id="fake-session", ok=True, text="edited", cost_usd=0.0, turns=1)

    def start(self, prompt: str, cwd: Path, run: CommandRunner) -> EngineResult:
        return self._step(prompt, cwd, None)

    def resume(self, session_id: Optional[str], prompt: str, cwd: Path, run: CommandRunner) -> EngineResult:
        return self._step(prompt, cwd, session_id)


def default_runner(argv: "list[str] | str", cwd: Path, timeout_s: int, env: Optional[dict] = None) -> tuple[int, str, str]:
    import os

    full_env = {**os.environ, **env} if env else None
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout_s,
                           env=full_env, shell=isinstance(argv, str))
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"timed out after {timeout_s}s"


ENGINES = {"claude": ClaudeEngine}


def get_engine(name: str, **kwargs) -> Engine:
    try:
        return ENGINES[name](**kwargs)
    except KeyError:
        raise ValueError(f"Unknown engine {name!r}. Available: {', '.join(sorted(ENGINES))}") from None
