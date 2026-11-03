"""Turn a SandboxPlan into an OS sandbox and run a command in it."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from chronos.sandbox.plan import SandboxPlan


class SandboxUnavailable(RuntimeError):
    """The platform or machine cannot provide the sandbox. Never fall back to running unprotected."""


def _require_bwrap() -> str:
    if not sys.platform.startswith("linux"):
        raise SandboxUnavailable(
            f"`chronos exec` currently supports Linux only (this is {sys.platform}). "
            "macOS (sandbox-exec) and Windows support are planned; hooks still apply without it."
        )
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SandboxUnavailable(
            "bubblewrap is not installed. Install it with your package manager, e.g. "
            "`sudo apt install bubblewrap` or `sudo dnf install bubblewrap`."
        )
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc", "true"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0:
        raise SandboxUnavailable(
            "bubblewrap cannot create a sandbox here (unprivileged user namespaces may be disabled): "
            + (probe.stderr.strip() or f"exit {probe.returncode}")
        )
    return bwrap


def build_command(plan: SandboxPlan, command: list[str], bwrap: str = "bwrap", chdir: str | None = None) -> list[str]:
    """The full bwrap argv. Order matters: anchors first, then read-only and hidden binds."""
    argv = [
        bwrap,
        "--bind", "/", "/",            # the agent sees the machine as usual ...
        "--dev-bind", "/dev", "/dev",  # ... including the real terminal (interactive agents)
        "--proc", "/proc",
        "--die-with-parent",
        "--setenv", "CHRONOS_SANDBOX", "1",
    ]
    for d in plan.anchors:
        argv += ["--bind", str(d), str(d)]
    for f in plan.read_only:
        argv += ["--ro-bind", str(f), str(f)]
    for f in plan.hidden:
        argv += ["--ro-bind", "/dev/null", str(f)]
    argv += ["--chdir", chdir or os.getcwd(), "--"]
    return argv + command


def run(plan: SandboxPlan, command: list[str]) -> int:
    """Run `command` in the sandbox, inheriting the terminal. Returns its exit code."""
    if not command:
        raise ValueError("no command given")
    bwrap = _require_bwrap()
    return subprocess.call(build_command(plan, command, bwrap))


def describe(plan: SandboxPlan, limit: int = 15) -> str:
    root = plan.repo_root
    lines = [f"Sandbox plan for {root}"]

    def section(title: str, paths: list[Path]) -> None:
        lines.append(f"  {title} ({len(paths)}):")
        for p in paths[:limit]:
            lines.append(f"    {p.relative_to(root).as_posix() if p != root else '.'}")
        if len(paths) > limit:
            lines.append(f"    ... {len(paths) - limit} more")

    section("read-only", plan.read_only)
    section("secrets, unreadable", plan.hidden)
    section("approved, left writable", plan.approved)
    lines.append(f"  anchored directories: {len(plan.anchors)} (cannot be renamed or replaced)")
    return "\n".join(lines)
