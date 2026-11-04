"""Add or remove the Chronos MCP server in an editor's JSON config without touching anything else.

Used by install.sh for Cursor (`~/.cursor/mcp.json`) and Claude Desktop
(`claude_desktop_config.json`). The previous installer overwrote these files, deleting every other
MCP server the user had configured.

Rules: keep every other key and server; back the file up once (`<file>.chronos-bak`) before the
first change; write atomically; and if the file exists but is not valid JSON, change nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path


class ConfigError(Exception):
    pass


def _load(path: Path) -> dict:
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON ({exc}); left unchanged. Add the chronos server by hand.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise ConfigError(f"{path} does not have the expected {{\"mcpServers\": {{...}}}} shape; left unchanged.")
    return data


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup = path.with_name(path.name + ".chronos-bak")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def add_server(path: str | Path, command: str, args: list[str] | None = None, name: str = "chronos") -> str:
    """Add (or update) one MCP server entry. Returns a one-line description of what happened."""
    path = Path(path).expanduser()
    data = _load(path)
    servers = data.setdefault("mcpServers", {})
    entry = {"command": command, "args": list(args or ["serve"])}
    if servers.get(name) == entry:
        return f"{path}: {name} already configured"
    action = "updated" if name in servers else "added"
    servers[name] = entry
    _write(path, data)
    others = [k for k in servers if k != name]
    return f"{path}: {name} {action}" + (f"; kept {len(others)} other server(s): {', '.join(others[:5])}" if others else "")


def remove_server(path: str | Path, name: str = "chronos") -> str:
    path = Path(path).expanduser()
    if not path.exists():
        return f"{path}: not present"
    data = _load(path)
    if name not in data.get("mcpServers", {}):
        return f"{path}: {name} not configured"
    del data["mcpServers"][name]
    _write(path, data)
    return f"{path}: {name} removed"
