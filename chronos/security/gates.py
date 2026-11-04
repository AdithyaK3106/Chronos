"""Sensitive module gates: human-in-the-loop lock approval (F5)."""

from datetime import datetime, timezone
import fnmatch
from pathlib import Path
import re
import sqlite3
from typing import Any

DEFAULT_PROTECTED_PATTERNS = [
    "*policy*",
    "*auth*",
    "*firewall*",
    "*security*",
    "*.env*",
    "*governor*",
    "*credentials*",
    "*permissions*",
]

# Default patterns match whole WORDS in path segments, not substrings: `*auth*` used to match
# `author.py` and `AUTHORS`. Each default pattern maps to the words that make a path sensitive.
# (Patterns written in .chronos/protected.yml keep plain glob semantics.)
DEFAULT_PROTECTED_WORDS: dict[str, set[str]] = {
    "*policy*": {"policy", "policies"},
    "*auth*": {"auth", "authn", "authz", "authentication", "authorization", "authorize", "oauth", "oauth2"},
    "*firewall*": {"firewall"},
    "*security*": {"security"},
    "*.env*": set(),  # handled by _is_env_file
    "*governor*": {"governor"},
    "*credentials*": {"credential", "credentials"},
    "*permissions*": {"permission", "permissions"},
}

_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def path_words(path: str) -> set[str]:
    """Lower-cased words of every path segment, split on punctuation and camelCase.

    `src/authService.ts` -> {src, auth, service, ts}; `author.py` -> {author, py}.
    """
    words: set[str] = set()
    for seg in Path(path).as_posix().split("/"):
        words.update(w.lower() for w in _WORD_RE.findall(seg))
    return words


def _is_env_file(path: str) -> bool:
    from chronos.security.sanitizer import is_env_template

    name = Path(path).name.lower()
    if is_env_template(name):
        return False
    return name == ".env" or name.startswith(".env.") or name.endswith(".env")


DOC_EXTENSIONS = {".md", ".markdown", ".mdx", ".rst", ".adoc", ".asciidoc"}
DOC_DIRS = {"docs", "doc", "documentation"}


def is_doc_path(path: str) -> bool:
    """Documentation, not code: `docs/oauth.md`, `SECURITY.md`, `auth/README.md`, `doc/api/auth.html`.

    The default protected words (auth, security, policy, ...) guard code and config; a page that
    describes the auth flow is not a security control.
    """
    parts = Path(path).as_posix().lower().split("/")
    return Path(parts[-1]).suffix in DOC_EXTENSIONS or any(seg in DOC_DIRS for seg in parts[:-1])


def _module_matches(mod: dict[str, Any], norm: str) -> str | None:
    """Return the pattern that makes `norm` protected under `mod`, or None."""
    if mod.get("words") is not None or mod.get("env"):
        if mod.get("env") and _is_env_file(norm):
            return mod.get("pattern", ".env")
        doc_exempt = mod.get("pattern") != "*credentials*"  # a credentials file is never "just docs"
        if mod.get("words") and not (doc_exempt and is_doc_path(norm)) and path_words(norm) & set(mod["words"]):
            return mod.get("pattern")
        return None
    for pat in mod.get("paths", []):
        if (
            fnmatch.fnmatch(norm, pat)
            or fnmatch.fnmatch(Path(norm).name, pat)
            or fnmatch.fnmatch(norm, f"**/{pat}")
        ):
            return pat
    return None


def find_protected_module(path_str: str, repo_root: str | Path) -> tuple[dict[str, Any], str] | None:
    """The protected module (and matching pattern) covering a path, or None. Single source of truth
    for the firewall, the MCP `chronos_check_gate` tool and the dashboard."""
    root = Path(repo_root).resolve()
    target = Path(path_str)
    try:
        norm = (target if target.is_absolute() else root / target).resolve().relative_to(root).as_posix()
    except Exception:
        norm = target.as_posix()
    for mod in load_protected_modules(root):
        pat = _module_matches(mod, norm)
        if pat:
            return mod, pat
    return None

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def grant_gate_approval(
    con: sqlite3.Connection,
    agent_id: str,
    path_pattern: str,
    approved_by: str = "admin",
    expires_at: str | None = None,
) -> None:
    """Grant explicit human/admin gate approval for an agent on a protected path pattern."""
    now = _utc_now_iso()
    with con:
        con.execute(
            """
            INSERT INTO gate_approvals (agent_id, path_pattern, approved_by, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent_id, path_pattern) DO UPDATE SET
                approved_by=excluded.approved_by,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at
            """,
            (agent_id, path_pattern, approved_by, now, expires_at),
        )

def revoke_gate_approval(
    con: sqlite3.Connection,
    agent_id: str,
    path_pattern: str,
) -> None:
    """Revoke gate approval for an agent on a path pattern."""
    with con:
        con.execute(
            "DELETE FROM gate_approvals WHERE agent_id = ? AND path_pattern = ?",
            (agent_id, path_pattern),
        )

def is_gate_approved(
    con: sqlite3.Connection | None,
    agent_id: str | None,
    path_str: str,
) -> bool:
    """Check if an active gate approval grant exists for agent on path."""
    if not con or not agent_id:
        return False

    norm = Path(path_str).as_posix()
    now = _utc_now_iso()
    try:
        rows = con.execute(
            """
            SELECT path_pattern FROM gate_approvals
            WHERE agent_id = ? AND (expires_at IS NULL OR expires_at > ?)
            """,
            (agent_id, now),
        ).fetchall()
        for r in rows:
            pat = r["path_pattern"] if isinstance(r, sqlite3.Row) else r[0]
            if (
                fnmatch.fnmatch(norm, pat)
                or fnmatch.fnmatch(Path(norm).name, pat)
                or fnmatch.fnmatch(norm, f"**/{pat}")
                or norm == pat
                or norm.endswith(pat.lstrip("/"))
            ):
                return True
        return False
    except Exception:
        # Fail closed on query failure
        return False

class ProtectedConfigError(ValueError):
    """`.chronos/protected.yml` has unknown keys or invalid values."""


TOP_LEVEL_KEYS = {"policies", "protected_modules", "exempt_patterns"}
POLICY_KEYS = {"id", "name", "paths", "mode", "denial_reason", "description"}
# `human_approval` is the name the docs used first; it means the same as `ask`.
POLICY_MODES = {"ask": "ask", "human_approval": "ask", "deny": "deny"}


def _parse_module(entry: Any, where: str, id_key: str) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ProtectedConfigError(f"{where}: expected a mapping, got {type(entry).__name__}")
    unknown = set(entry) - POLICY_KEYS
    if unknown:
        raise ProtectedConfigError(
            f"{where}: unknown key(s) {', '.join(sorted(map(str, unknown)))} (allowed: {', '.join(sorted(POLICY_KEYS))})"
        )
    name = entry.get(id_key) or entry.get("name") or entry.get("id")
    if not name or not isinstance(name, str):
        raise ProtectedConfigError(f"{where}: needs a string `{id_key}`")
    paths = entry.get("paths")
    if not isinstance(paths, list) or not paths or not all(isinstance(x, str) and x.strip() for x in paths):
        raise ProtectedConfigError(f"{where} ({name}): `paths` must be a non-empty list of glob strings")
    mode = entry.get("mode", "ask")
    if mode not in POLICY_MODES:
        raise ProtectedConfigError(f"{where} ({name}): mode {mode!r} is not one of ask, deny, human_approval")
    reason = entry.get("denial_reason")
    if reason is not None and not isinstance(reason, str):
        raise ProtectedConfigError(f"{where} ({name}): `denial_reason` must be a string")
    mod: dict[str, Any] = {"name": name, "paths": list(paths), "mode": POLICY_MODES[mode]}
    if reason:
        mod["denial_reason"] = reason.strip()
    return mod


def parse_protected_config(data: Any) -> tuple[list[dict[str, Any]], set[str]]:
    """Validate a parsed protected.yml. Returns (custom modules, exempt patterns).

    Two shapes are accepted and may be combined: the documented `policies:` list (`id`, `paths`,
    `mode`, `denial_reason`) and the older `protected_modules:` list (`name`, `paths`). Unknown keys
    raise ProtectedConfigError: a typo such as `path:` or `deny_reason:` must not silently leave a
    module unprotected.
    """
    if data is None:
        return [], set()
    if not isinstance(data, dict):
        raise ProtectedConfigError("protected.yml must be a mapping at the top level")
    unknown = set(data) - TOP_LEVEL_KEYS
    if unknown:
        raise ProtectedConfigError(
            f"unknown top-level key(s) {', '.join(sorted(map(str, unknown)))} (allowed: {', '.join(sorted(TOP_LEVEL_KEYS))})"
        )
    modules: list[dict[str, Any]] = []
    for key, id_key in (("policies", "id"), ("protected_modules", "name")):
        entries = data.get(key) or []
        if not isinstance(entries, list):
            raise ProtectedConfigError(f"`{key}` must be a list")
        modules += [_parse_module(e, f"{key}[{i}]", id_key) for i, e in enumerate(entries)]
    exempt = data.get("exempt_patterns") or []
    if not isinstance(exempt, list) or not all(isinstance(x, str) for x in exempt):
        raise ProtectedConfigError("`exempt_patterns` must be a list of strings")
    return modules, set(exempt)


def protected_config_error(repo_root: str | Path) -> str | None:
    """Why `.chronos/protected.yml` was refused, or None if it is absent or valid (for `chronos doctor`)."""
    cfg_file = Path(repo_root).resolve() / ".chronos" / "protected.yml"
    if not cfg_file.is_file():
        return None
    try:
        import yaml

        parse_protected_config(yaml.safe_load(cfg_file.read_text(encoding="utf-8")))
    except ProtectedConfigError as exc:
        return f"{cfg_file}: {exc}"
    except Exception as exc:  # noqa: BLE001 - YAML syntax errors, unreadable file
        return f"{cfg_file}: could not be read ({type(exc).__name__}: {exc})"
    return None


def load_protected_modules(repo_root: str | Path) -> list[dict[str, Any]]:
    """Load protected modules from .chronos/protected.yml or fallback to default safety patterns."""
    cfg_file = Path(repo_root).resolve() / ".chronos" / "protected.yml"
    default_modules = [
        {
            "name": f"default_safety_{pat.strip('*')}",
            "paths": [f"**/{pat}", f"{pat}", pat],  # kept for exempt_patterns compatibility
            "pattern": pat,
            "words": sorted(DEFAULT_PROTECTED_WORDS.get(pat, set())),
            "env": pat == "*.env*",
        }
        for pat in DEFAULT_PROTECTED_PATTERNS
    ]

    if not cfg_file.is_file():
        # Fail closed: default safety-critical modules protected even without config
        return default_modules

    try:
        import yaml  # only when the repo has a protected.yml (saves ~20 ms per hook call)

        custom_modules, exempt_patterns = parse_protected_config(yaml.safe_load(cfg_file.read_text(encoding="utf-8")))
    except Exception as exc:  # noqa: BLE001
        # Refuse to load a bad config, and fail closed: every edit needs approval until it is fixed.
        return [{
            "name": "invalid_protected_config",
            "paths": ["**"],
            "error": str(exc),
            "denial_reason": f".chronos/protected.yml is invalid ({exc}); every edit needs approval until it is fixed. Run `chronos doctor`",
        }]
    # Merge custom with defaults (unless explicitly exempted)
    active_defaults = [
        m for m in default_modules
        if not any(fnmatch.fnmatch(p, ex) for p in m["paths"] for ex in exempt_patterns)
    ]
    return custom_modules + active_defaults


def check_gate_approval(
    path_str: str,
    repo_root: str | Path,
    con: sqlite3.Connection | None = None,
    agent_id: str | None = None,
) -> tuple[bool, str]:
    """Check if path falls under a protected module requiring human gate approval."""
    root = Path(repo_root).resolve()
    target = Path(path_str)
    if target.is_absolute():
        try:
            norm = target.resolve().relative_to(root).as_posix()
        except ValueError:
            norm = target.as_posix()
    else:
        try:
            norm = (root / target).resolve().relative_to(root).as_posix()
        except Exception:
            norm = target.as_posix()

    match = find_protected_module(norm, root)
    if match:
        mod, pat = match
        if is_gate_approved(con, agent_id, norm):
            return True, f"Approved under active gate grant for '{norm}'"
        msg = f"Path '{norm}' requires human gate approval under protected policy '{mod.get('name', pat)}'"
        if mod.get("denial_reason"):
            msg += f": {mod['denial_reason']}"
        return False, msg

    return True, "Approved"
