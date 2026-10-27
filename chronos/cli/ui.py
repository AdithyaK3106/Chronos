"""Chronos zero-configuration, repository-aware UI launcher."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Optional

from chronos.core.repo import RepoIdentity, canonicalize_path, inspect_repository
from chronos.server.dashboard_server import (
    clear_server_state,
    get_state_file_path,
    is_port_in_use,
    run_server,
)


def _get_launcher_lock_path() -> Path:
    chronos_home = Path.home() / ".chronos"
    chronos_home.mkdir(parents=True, exist_ok=True)
    return chronos_home / "launcher.lock"


class LauncherLock:
    """Inter-process file lock preventing duplicate simultaneous server spawns."""

    def __init__(self):
        self.lock_path = _get_launcher_lock_path()
        self._fd: Optional[int] = None

    def acquire(self, timeout: float = 5.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                # O_CREAT | O_EXCL provides atomic file creation
                self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                return True
            except (FileExistsError, OSError):
                # Stale lock check (older than 15s)
                try:
                    if self.lock_path.is_file():
                        mtime = self.lock_path.stat().st_mtime
                        if time.time() - mtime > 15.0:
                            self.lock_path.unlink(missing_ok=True)
                except Exception:
                    pass
                time.sleep(0.1)
        return False

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None
        try:
            if self.lock_path.is_file():
                self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


def probe_health(host: str, port: int, timeout: float = 0.5) -> Optional[dict]:
    """Probe the health check endpoint of a potentially running Chronos dashboard."""
    url = f"http://{host}:{port}/api/health"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Chronos-Launcher/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("app") == "chronos-ui":
                    return data
    except Exception:
        pass
    return None


def register_repo_with_server(host: str, port: int, canonical_root: str) -> Optional[dict]:
    """Register repository path with a running Chronos dashboard server."""
    url = f"http://{host}:{port}/api/repos/register"
    data = json.dumps({"path": canonical_root}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "Chronos-Launcher/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def stop_running_server() -> bool:
    """Terminate the active managed Chronos UI server if running."""
    state_file = get_state_file_path()
    if not state_file.is_file():
        return False

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        pid = data.get("pid")
        if not pid:
            clear_server_state()
            return False

        # Attempt termination
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, 15)  # SIGTERM
            except ProcessLookupError:
                pass
        clear_server_state()
        return True
    except Exception:
        clear_server_state()
        return False


def wait_for_server_ready(host: str, port: int, max_seconds: float = 10.0) -> bool:
    """Poll health endpoint until server is confirmed ready."""
    deadline = time.time() + max_seconds
    while time.time() < deadline:
        if probe_health(host, port, timeout=0.2):
            return True
        time.sleep(0.15)
    return False


def format_terminal_output(repo: RepoIdentity, dashboard_url: str, is_reused: bool = False, open_browser: bool = True) -> str:
    """Format standard terminal announcement matching the MLflow-inspired specification."""
    status_str = "Ready" if repo.is_indexed else "Unindexed"
    lines = [
        "Chronos Control Plane",
        "",
        f"Repository: {repo.name}",
        f"Root:       {repo.canonical_root}",
        f"Index:      {status_str}",
        f"Dashboard:  {dashboard_url}",
        "",
    ]
    if open_browser:
        lines.append("Opening dashboard...")
    return "\n".join(lines)


def launch_ui(
    repo_path: str = ".",
    host: str = "127.0.0.1",
    port: int = 8765,
    no_browser: bool = False,
    foreground: bool = False,
    stop: bool = False,
) -> int:
    """Zero-configuration entrypoint to launch or connect to the Chronos Control Plane."""
    if stop:
        stopped = stop_running_server()
        if stopped:
            print("Chronos dashboard server stopped.")
        else:
            print("No active managed Chronos dashboard server found.")
        return 0

    # 1. Resolve and inspect the target repository
    try:
        repo_info = inspect_repository(repo_path)
    except Exception as e:
        print(f"Error inspecting repository '{repo_path}': {e}", file=sys.stderr)
        return 1

    dashboard_url = f"http://{host}:{port}/repo/{repo_info.id}"

    # 2. Check if a compatible Chronos UI server is already running
    running_health = probe_health(host, port)

    if running_health:
        # Existing server detected! Reuse it.
        register_repo_with_server(host, port, repo_info.canonical_root)
        print(format_terminal_output(repo_info, dashboard_url, is_reused=True, open_browser=not no_browser))
        if not no_browser:
            webbrowser.open(dashboard_url)
        return 0

    # 3. Check for port conflicts with unrelated processes
    if is_port_in_use(host, port):
        print(
            f"Error: Port {port} on {host} is already in use by another process.\n"
            f"Please specify a different port using --port <port>.",
            file=sys.stderr,
        )
        return 1

    # 4. Starting a new server instance
    with LauncherLock():
        # Double-check after acquiring lock in case of race condition
        running_health = probe_health(host, port)
        if running_health:
            register_repo_with_server(host, port, repo_info.canonical_root)
            print(format_terminal_output(repo_info, dashboard_url, is_reused=True, open_browser=not no_browser))
            if not no_browser:
                webbrowser.open(dashboard_url)
            return 0

        if foreground:
            print(format_terminal_output(repo_info, dashboard_url, is_reused=False, open_browser=not no_browser))
            if not no_browser:
                # Open browser in separate thread once ready
                def _open_when_ready():
                    if wait_for_server_ready(host, port, max_seconds=5.0):
                        webbrowser.open(dashboard_url)

                threading.Thread(target=_open_when_ready, daemon=True).start()
            run_server(host=host, port=port, initial_repo=repo_info.canonical_root)
            return 0

        # Spawn detached background server process
        server_cmd = [
            sys.executable,
            "-m",
            "chronos.server.dashboard_server",
            "--host",
            host,
            "--port",
            str(port),
            "--repo",
            repo_info.canonical_root,
        ]

        popen_kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }

        if sys.platform == "win32":
            # CREATE_NO_WINDOW = 0x08000000
            popen_kwargs["creationflags"] = 0x08000000
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(server_cmd, **popen_kwargs)
        except Exception as e:
            print(f"Error launching Chronos dashboard server: {e}", file=sys.stderr)
            return 1

        # Wait for server readiness health check
        ready = wait_for_server_ready(host, port, max_seconds=10.0)
        if not ready:
            print(
                f"Error: Chronos dashboard server process (PID {proc.pid}) failed to respond within 10s.\n"
                f"Please check system permissions or run with --foreground for detailed diagnostics.",
                file=sys.stderr,
            )
            return 1

        # Register repository and launch browser
        register_repo_with_server(host, port, repo_info.canonical_root)
        print(format_terminal_output(repo_info, dashboard_url, is_reused=False, open_browser=not no_browser))
        if not no_browser:
            webbrowser.open(dashboard_url)
        return 0


def add_ui_subparser(subparsers) -> None:
    """Register the 'ui' subcommand into the main CLI parser."""
    p_ui = subparsers.add_parser(
        "ui",
        help="Launch zero-configuration, repository-aware Chronos Control Plane dashboard",
        description="Launch or reuse local Chronos engineering control plane and open in browser.",
    )
    p_ui.add_argument("--port", type=int, default=8765, help="Port to bind dashboard server (default: 8765)")
    p_ui.add_argument("--host", default="127.0.0.1", help="Host address to bind dashboard server (default: 127.0.0.1)")
    p_ui.add_argument("--repo", default=".", help="Repository root path (default: current working directory)")
    p_ui.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")
    p_ui.add_argument("--foreground", action="store_true", help="Run dashboard server in the foreground")
    p_ui.add_argument("--stop", action="store_true", help="Stop running Chronos dashboard server")
