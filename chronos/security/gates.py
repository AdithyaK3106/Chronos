"""Sensitive module gates: human-in-the-loop lock approval (F5)."""

import fnmatch
from pathlib import Path
from typing import Any
import yaml

def load_protected_modules(repo_root: str | Path) -> list[dict[str, Any]]:
    """Load protected modules from .chronos/protected.yml. Fails closed."""
    cfg_file = Path(repo_root).resolve() / ".chronos" / "protected.yml"
    if not cfg_file.is_file():
        return []
    try:
        data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
        return data.get("protected_modules", [])
    except Exception:
        # Fail closed: if config file exists but is corrupted, protect everything
        return [{"name": "corrupted_config_safeguard", "paths": ["**"]}]

def check_gate_approval(path_str: str, repo_root: str | Path) -> tuple[bool, str]:
    """Check if path falls under a protected module requiring human gate approval."""
    norm = Path(path_str).as_posix()
    modules = load_protected_modules(repo_root)

    for mod in modules:
        for pat in mod.get("paths", []):
            if fnmatch.fnmatch(norm, pat):
                return False, f"Path '{norm}' requires human gate approval under protected policy '{mod.get('name', pat)}'"

    return True, "Approved"
