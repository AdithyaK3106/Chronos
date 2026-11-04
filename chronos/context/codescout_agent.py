"""Run OpenHands/CodeScout the way it was trained: a short search loop with a read-only terminal.

CodeScout (arXiv 2603.17829) is RL-trained inside OpenHands with two tools, `terminal` (shell
commands such as `rg`, `find`, `sed -n`) and `localization_finish` (the answer), for up to 6 turns.
Asked for a one-shot JSON answer instead, it tries to call a tool, so the old one-shot integration
in `slm_resolver.py` returned nothing usable.

This loop talks to an Ollama server (`/api/chat` with tools). Every command the model proposes is
checked before it runs: only read-only search programs, no redirections or writes, nothing that
names a secret file, nothing Chronos can't analyse. Commands run in the repository with a short
timeout and their output is truncated.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The prompts CodeScout was trained with (OpenHands/codescout, templates/*_custom_finish.j2), abridged.
SYSTEM_PROMPT = """You are a specialized code localization agent. Your sole objective is to identify and return the files in the codebase that are relevant to the user's query.
You are given access to the codebase in a linux file system.

## PRIMARY DIRECTIVE
- Find relevant files, do NOT answer the user's query directly
- Prioritize precision: every file you return should be relevant
- You have up to {turns} turns to explore and return your answer

## TOOL USAGE REQUIREMENTS
- You MUST use the terminal tool to search and explore the codebase: rg, grep, find, ls, cat, head, tail, sed -n, wc
- Use parallel tool calls: up to 5 terminal calls in a single turn
- NEVER read entire large files with cat; check size with wc -l and read ranges with sed -n 'START,ENDp'

## Submitting Your Answer (REQUIRED)
When you have identified all relevant locations, you MUST use the `localization_finish` tool. Give the file path,
and the class and function names when the change belongs to them."""

USER_PROMPT = """I have access to a code repository in the directory {working_dir} . Consider the following issue description:

<issue_description>
{issue}
</issue_description>

Act as a code search agent and localize the specific files, classes or functions of code that need modification to resolve the issue in <issue_description>.

NOTE: You do not need to solve the issue, all you need to do is localize relevant code from the repository. Your output will be used to guide another agent to solve the issue.

IMPORTANT: Your output MUST follow the below rules:
1. The final output must be a tool call to the "localization_finish" tool containing relevant code locations.
2. The locations of the file path must be RELATIVE to the {working_dir} directory WITHOUT any leading "./" in the output.
3. Only include those locations in your output that need modification to resolve the issue in <issue_description>. Do NOT include any locations that do not need modification."""

TOOLS = [
    {"type": "function", "function": {
        "name": "terminal",
        "description": "Execute a read-only shell command in the repository (rg, grep, find, ls, cat, head, tail, sed -n, wc).",
        "parameters": {"type": "object", "properties": {"command": {"type": "string", "description": "The command to run"}}, "required": ["command"]},
    }},
    {"type": "function", "function": {
        "name": "localization_finish",
        "description": "Submit the final list of code locations that need modification.",
        "parameters": {"type": "object", "properties": {"locations": {"type": "array", "items": {"type": "object", "properties": {
            "file": {"type": "string", "description": "Path relative to the repository root"},
            "class_name": {"type": "string", "description": "Class name (optional)"},
            "function_name": {"type": "string", "description": "Function or method name (optional)"},
        }, "required": ["file"]}}}, "required": ["locations"]},
    }},
]

READ_ONLY = {"rg", "grep", "egrep", "fgrep", "find", "ls", "cat", "head", "tail", "sed", "wc", "tree", "sort", "uniq", "cut", "awk", "nl", "file", "basename", "dirname", "xargs", "echo", "pwd", "true"}
MAX_OBSERVATION = 6000


@dataclass
class ScoutResult:
    locations: list[dict[str, Any]] = field(default_factory=list)
    turns: int = 0
    commands: list[str] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)
    seconds: float = 0.0
    prompt_tokens: int = 0
    error: str | None = None

    @property
    def files(self) -> list[str]:
        out: list[str] = []
        for loc in self.locations:
            f = str(loc.get("file") or "").removeprefix("./")
            if f and f not in out:
                out.append(f)
        return out

    @property
    def symbols(self) -> list[str]:
        out = []
        for loc in self.locations:
            parts = [p for p in (loc.get("class_name"), loc.get("function_name")) if p]
            if parts:
                out.append(".".join(parts))
        return out


def check_command(command: str, root: Path) -> str | None:
    """Why a model-proposed command may not run, or None if it is a safe read-only search."""
    from chronos.security.shell import analyze

    a = analyze(command, root)
    if a.secrets:
        return "names secret files"
    if a.writes or a.hidden:
        return "writes files or hides what it runs"
    for c in a.commands:
        if c.prog not in READ_ONLY:
            return f"{c.prog} is not a read-only search command"
        if c.prog == "sed" and any(x.startswith(("-i", "--in-place")) for x in c.args):
            return "sed -i edits files"
        if c.prog == "find" and any(x in ("-exec", "-execdir", "-delete", "-ok", "-fprint") for x in c.args):
            return "find -exec/-delete"
        if c.prog == "xargs" or "xargs" in c.prefixes:
            inner = c.words[0] if c.words else ""
            if Path(inner).name not in READ_ONLY:
                return "xargs runs a non-search command"
        if c.prog == "awk" and any("system" in x or ">" in x for x in c.args):
            return "awk script runs commands or writes"
    if "`" in command or "$(" in command:
        return "command substitution"
    return None


def _run(command: str, root: Path, timeout: float = 10.0) -> str:
    why = check_command(command, root)
    if why:
        return f"[Command refused by Chronos: {why}]"
    try:
        r = subprocess.run(["bash", "-c", command], cwd=root, capture_output=True, text=True, timeout=timeout,
                           env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(root), "LC_ALL": "C.UTF-8"})
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr[-500:]) if r.stderr.strip() else "")
    except subprocess.TimeoutExpired:
        out = "[timed out]"
    if len(out) > MAX_OBSERVATION:
        out = out[:MAX_OBSERVATION] + f"\n[... truncated, {len(out)} chars total]"
    return out or "[no output]"


def _chat(endpoint: str, model: str, messages: list[dict], num_ctx: int, timeout: float) -> dict:
    body = {"model": model, "messages": messages, "tools": TOOLS, "stream": False,
            "options": {"temperature": 0, "num_ctx": num_ctx}, "think": False}
    req = urllib.request.Request(endpoint.rstrip("/") + "/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def scout(prompt: str, root: str | Path, endpoint: str = "http://127.0.0.1:11434", model: str = "codescout-1.7b",
          max_turns: int = 6, num_ctx: int = 12288, deadline_s: float = 300.0) -> ScoutResult:
    """Localize `prompt` in the repository at `root`."""
    root = Path(root).resolve()
    res = ScoutResult()
    started = time.monotonic()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT.format(turns=max_turns)},
        {"role": "user", "content": USER_PROMPT.format(working_dir=str(root), issue=prompt)},
    ]
    try:
        for turn in range(max_turns):
            left = deadline_s - (time.monotonic() - started)
            if left <= 5:
                res.error = "deadline"
                break
            if turn == max_turns - 1:
                messages.append({"role": "user", "content": "This is your last turn: call localization_finish now."})
            r = _chat(endpoint, model, messages, num_ctx, timeout=left)
            res.turns = turn + 1
            res.prompt_tokens += int(r.get("prompt_eval_count") or 0)
            msg = r.get("message") or {}
            calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            if not calls:
                messages.append({"role": "user", "content": "Use the terminal tool to search, or call localization_finish with your answer."})
                continue
            for call in calls[:5]:
                fn = call.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if fn.get("name") == "localization_finish":
                    locs = args.get("locations") or []
                    res.locations = [l for l in locs if isinstance(l, dict) and l.get("file")]
                    res.seconds = time.monotonic() - started
                    return res
                cmd = str(args.get("command") or "")
                res.commands.append(cmd)
                obs = _run(cmd, root)
                if obs.startswith("[Command refused"):
                    res.refused.append(cmd)
                messages.append({"role": "tool", "content": obs, "tool_name": "terminal"})
    except Exception as exc:  # noqa: BLE001
        res.error = f"{type(exc).__name__}: {exc}"
    res.seconds = time.monotonic() - started
    return res
