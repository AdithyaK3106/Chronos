"""Path sanitization, traversal defense, symlink escape detection, and secret scrubbing."""

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from chronos.core.exceptions import PathTraversalError

# Sensitive patterns that must never be read or written without explicit policy
DEFAULT_DENIED_PATTERNS = [
    ".env*",
    "*.env*",
    "**/.env*",
    "**/*.env*",
    "**/secrets/**",
    "**/id_rsa*",
    "**/*.pem",
    "**/*.key",
    "**/credentials*",
    "**/.git/**",
    "**/.chronos/chronos.db*",
]

# Regex patterns for credential redacting
SECRET_PATTERNS = [
    (re.compile(r"chron_[a-zA-Z0-9_\-]{20,}"), "chron_[REDACTED]"),
    (re.compile(r"sk-[a-zA-Z0-9_\-]{20,}"), "sk-[REDACTED]"),
    (re.compile(r"ghp_[a-zA-Z0-9_\-]{20,}"), "ghp_[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9\-\._~\+\/]+=*", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]+?-----END [A-Z ]+ PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r'(password|secret|token|api_key|apikey)["\']?\s*[:=]\s*["\']?([^"\'\s]+)', re.IGNORECASE), r'\1: "[REDACTED]"'),
]

def sanitize_and_resolve_path(target_path_str: str, repo_root: str | Path) -> Path:
    """Canonicalize a target path and verify it remains strictly within repo_root."""
    root = Path(repo_root).resolve()
    target_path = Path(target_path_str)

    if target_path.is_absolute():
        resolved = target_path.resolve()
    else:
        resolved = (root / target_path).resolve()

    try:
        resolved.relative_to(root)
    except ValueError:
        raise PathTraversalError(
            f"Path traversal detected: '{target_path_str}' escapes repository root '{root}'"
        )

    return resolved

def validate_path_access(
    target_path_str: str,
    repo_root: str | Path,
    allowed_globs: list[str] | None = None,
    denied_globs: list[str] | None = None,
) -> tuple[bool, str]:
    """Validate that target_path_str is safe from traversal, symlink escapes, and matches permissions."""
    root = Path(repo_root).resolve()
    allowed = allowed_globs if allowed_globs is not None else ["**"]
    denied = list(set((denied_globs or []) + DEFAULT_DENIED_PATTERNS))

    try:
        resolved = sanitize_and_resolve_path(target_path_str, root)
    except PathTraversalError as e:
        return False, str(e)
    except Exception as e:
        return False, f"Invalid path resolution: {e}"

    rel_posix = resolved.relative_to(root).as_posix()
    target_posix = Path(target_path_str).as_posix()

    # 1. Deny check on nominal path
    for pat in denied:
        if fnmatch.fnmatch(target_posix, pat) or fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(resolved.name, pat):
            return False, f"Access denied: path matches restricted pattern '{pat}'"

    # 2. Symlink escape check: if the path or parent is a symlink, verify real target stays inside root
    if resolved.exists():
        real_path = resolved.resolve()
        try:
            real_rel_posix = real_path.relative_to(root).as_posix()
        except ValueError:
            return False, f"Symlink escape detected: '{target_path_str}' points outside repository root"

        for pat in denied:
            if fnmatch.fnmatch(real_rel_posix, pat) or fnmatch.fnmatch(real_path.name, pat):
                return False, f"Symlink escape: real target matches restricted pattern '{pat}'"

    # 3. Allow check
    matched_allowed = any(fnmatch.fnmatch(rel_posix, pat) for pat in allowed)
    if not matched_allowed:
        return False, f"Access denied: path '{rel_posix}' is not in allowed_paths {allowed}"

    return True, "Allowed"

def redact_secrets(val: Any) -> Any:
    """Recursively scrub secrets from strings, dictionaries, or lists."""
    if isinstance(val, str):
        cleaned = val
        for pat, repl in SECRET_PATTERNS:
            cleaned = pat.sub(repl, cleaned)
        return cleaned
    elif isinstance(val, dict):
        return {k: redact_secrets(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [redact_secrets(item) for item in val]
    return val
