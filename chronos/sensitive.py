"""Sensitive-path classification for read tracking (F7).

Pure classification: never touches file content, only the path string.
.chronos/sensitive.yml is re-read on mtime change so a platform engineer's
edits take effect without a server restart.
"""

import fnmatch
from pathlib import Path

import yaml

DEFAULT_PATTERNS = [
    {"label": "secrets", "severity": "high",
     "patterns": [".env", ".env.*", "*secret*", "*credential*", "*password*",
                  "*api_key*", "*.pem", "*.p12", "*.pfx", "id_rsa", "id_ed25519", "*.key"]},
]

_cache = {"mtime": None, "patterns": DEFAULT_PATTERNS, "path": None}


def _config_path() -> Path:
    import os
    root = os.environ.get("CHRONOS_REPO_PATH") or "."
    return Path(root).resolve() / ".chronos" / "sensitive.yml"


def _load() -> list[dict]:
    p = _config_path()
    if not p.is_file():
        return DEFAULT_PATTERNS
    mtime = p.stat().st_mtime
    if _cache["path"] == p and _cache["mtime"] == mtime:
        return _cache["patterns"]
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        patterns = doc.get("sensitive_patterns") or DEFAULT_PATTERNS
    except (OSError, yaml.YAMLError):
        patterns = DEFAULT_PATTERNS
    _cache.update(mtime=mtime, patterns=patterns, path=p)
    return patterns


def classify_path(path: str) -> dict | None:
    """{"label", "severity", "pattern_matched"} or None."""
    if not path:
        return None
    norm = path.replace("\\", "/")
    for group in _load():
        for pat in group.get("patterns", []):
            glob = pat if "/" in pat or "**" in pat else f"*/{pat}"
            if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, glob) or fnmatch.fnmatch(Path(norm).name, pat):
                return {"label": group["label"], "severity": group["severity"], "pattern_matched": pat}
    return None
