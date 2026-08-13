"""Git-native rule distribution — the default path when Packmind is unset.

A proposed rule becomes a YAML file in .chronos/rules/, a branch, and a draft
PR. The PR is the approval gate, exactly as not-publishing is the gate on the
Packmind path (playbook.py [D2]). Merging it approves the rule; `chronos
approve-rule <id>` then advances it to warn-only enforcement.

Every git/gh call is check=False with returncode inspected by hand. A repo with
no remote, no gh, or no git at all must degrade to "file written, no PR" — a
lost lesson is worse than a missing PR, and the curator must never crash because
the developer's git is in an odd state.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import rule_store

BRANCH_PREFIX = "chronos/rule-"


def _log(msg):
    """stderr: stdout belongs to MCP's JSON protocol."""
    print(msg, file=sys.stderr)


def rule_id_for(candidate) -> str:
    """Stable id from the rule text.

    CandidateRule has no id (see curator.py), and a random one would open a new
    branch and a new PR every time the same lesson is learned. Hashing the text
    makes re-proposal idempotent: same rule, same branch, same file."""
    if candidate.get("rule_id"):
        return candidate["rule_id"]
    digest = hashlib.sha256(candidate.get("rule_text", "").encode()).hexdigest()[:8]
    return f"chronos-{digest}"


def _run(args, cwd):
    return subprocess.run(args, cwd=cwd, check=False, capture_output=True, text=True)


def _git(args, repo_path):
    return _run(["git", "-C", str(repo_path)] + args, cwd=None)


def _short_name(rule_text) -> str:
    one = " ".join((rule_text or "").split())
    return one[:80] or "chronos rule"


def resolve_repo_path(explicit=None) -> str:
    """explicit -> CHRONOS_REPO_PATH -> .chronos/config.json "repo" -> cwd."""
    if explicit:
        return str(explicit)
    env = os.environ.get("CHRONOS_REPO_PATH")
    if env:
        return env
    sqlite = os.environ.get("CHRONOS_SQLITE")
    if sqlite:
        cfg = Path(sqlite).parent / "config.json"
        try:
            repo = json.loads(cfg.read_text(encoding="utf-8")).get("repo")
            if repo:
                return repo
        except (OSError, json.JSONDecodeError):
            pass  # unreadable config is not fatal; fall through to cwd
    return os.getcwd()


def _write_rule_file(repo_path, rule_id, candidate):
    """Write the rule YAML unless rule_generator.py already produced it."""
    rules_dir = Path(repo_path) / ".chronos" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)
    path = rules_dir / f"{rule_id}.yml"
    if path.exists():
        return path, False  # generator's pattern wins; never clobber it
    pattern = candidate.get("yaml_pattern")
    if not pattern:
        # No executable pattern yet (Wedge 4 generates those). Record the rule
        # as prose so the PR still carries reviewable content.
        text = (candidate.get("rule_text") or "").replace("\n", "\n#   ")
        pattern = (f"# Chronos proposed rule — no ast-grep pattern yet.\n"
                   f"# Run chronos generate-rule to make it executable.\n"
                   f"id: {rule_id}\n"
                   f"# rule: {text}\n")
    path.write_text(pattern, encoding="utf-8")
    return path, True


def _current_branch(repo_path):
    r = _git(["rev-parse", "--abbrev-ref", "HEAD"], repo_path)
    return r.stdout.strip() if r.returncode == 0 else None


def _checkout_branch(repo_path, branch):
    """New branch, or switch to it if it already exists. False if git is absent."""
    r = _git(["checkout", "-b", branch], repo_path)
    if r.returncode == 0:
        return True
    if "already exists" in (r.stderr or ""):
        return _git(["checkout", branch], repo_path).returncode == 0
    return False


def _commit(repo_path, rel_path, rule_id, name):
    add = _git(["add", "--", rel_path], repo_path)
    if add.returncode != 0:
        return False
    r = _git(["commit", "-m", f"chronos: propose rule {rule_id} — {name}"], repo_path)
    if r.returncode == 0:
        return True
    combined = (r.stdout or "") + (r.stderr or "")
    if "nothing to commit" in combined or "no changes added" in combined:
        return False  # re-proposal of an unchanged rule; not an error
    _log(f"[Chronos] commit failed: {combined.strip()[:200]}")
    return False


PR_BODY = """## Chronos proposed rule

**Rule:** {name}
**Language:** {language}
**Confidence:** {confidence}

### Evidence
{evidence}

### Pattern
```yaml
{pattern_content}
```

*Proposed by Chronos Reflector. Merge to promote to warn-only enforcement.*
*Run `python -m chronos approve-rule {rule_id}` after merging to activate it.*
"""


def _evidence_block(candidate):
    node = candidate.get("evidence_node") or "(none)"
    valid_at = candidate.get("evidence_valid_at") or "(unknown)"
    ctx = candidate.get("evidence_commit_context") or "(none)"
    agent = candidate.get("agent_id") or "(unknown)"
    return (f"- **Node:** `{node}`\n"
            f"- **Last changed:** {valid_at}\n"
            f"- **Graph context:** {ctx}\n"
            f"- **Learned from agent:** {agent}")


def _open_pr(repo_path, branch, rule_id, candidate, pattern_content):
    """Push and open a draft PR. None if anything is missing — never fatal."""
    push = _git(["push", "-u", "origin", branch], repo_path)
    if push.returncode != 0:
        _log(f"[Chronos] push failed: {(push.stderr or '').strip()[:200]}")
        return None
    if shutil.which("gh") is None:
        return None
    name = _short_name(candidate.get("rule_text"))
    body = PR_BODY.format(
        name=name,
        language=candidate.get("language") or "unknown",
        confidence=candidate.get("confidence", "n/a"),
        evidence=_evidence_block(candidate),
        pattern_content=pattern_content,
        rule_id=rule_id,
    )
    r = _run(["gh", "pr", "create", "--draft", "--title", f"Chronos: {name}",
              "--body", body, "--head", branch], cwd=str(repo_path))
    if r.returncode != 0:
        _log(f"[Chronos] gh pr create failed: {(r.stderr or '').strip()[:200]}")
        return None
    m = re.search(r"https?://\S+", r.stdout or "")
    return m.group(0) if m else None


def submit_git_native(candidate: dict, repo_path: str) -> dict:
    """Write the rule, branch, commit, draft-PR it, and record it as 'proposed'.

    Returns {"status": "proposed", "branch": str|None, "pr_url": str|None,
             "rule_id": str, "rule_path": str}."""
    repo_path = resolve_repo_path(repo_path)
    rule_id = rule_id_for(candidate)
    name = _short_name(candidate.get("rule_text"))

    path, _ = _write_rule_file(repo_path, rule_id, candidate)
    pattern_content = path.read_text(encoding="utf-8")
    rel_path = str(Path(".chronos") / "rules" / f"{rule_id}.yml")

    branch = f"{BRANCH_PREFIX}{rule_id}"
    pr_url = None
    original = _current_branch(repo_path)
    have_git = original is not None

    if not have_git:
        _log("[Chronos] git unavailable or not a repo — rule file written, no PR")
        branch = None
    else:
        if _checkout_branch(repo_path, branch):
            if _commit(repo_path, rel_path, rule_id, name):
                pr_url = _open_pr(repo_path, branch, rule_id, candidate, pattern_content)
            # Leave the developer where we found them. Being silently moved to
            # a chronos branch mid-task is a nasty surprise.
            if original and original != branch:
                _git(["checkout", original], repo_path)
        else:
            _log(f"[Chronos] could not create branch {branch} — rule file written, no PR")
            branch = None

    # Recorded regardless of git outcome: the lesson is not lost because the
    # developer has no remote.
    rule_store.propose_rule(
        rule_id,
        candidate.get("language") or "unknown",
        candidate.get("rule_text") or "",
        candidate.get("yaml_pattern"),
    )

    _log(f"[Chronos] Rule proposed: {name}")
    _log(f"[Chronos] Branch: {branch or '(none)'}")
    _log(f"[Chronos] PR: {pr_url}" if pr_url
         else "[Chronos] No PR created — gh CLI not found or push failed")
    _log("[Chronos] To approve: merge the PR, then run:")
    _log(f"  python -m chronos approve-rule {rule_id}")

    return {"status": "proposed", "branch": branch, "pr_url": pr_url,
            "rule_id": rule_id, "rule_path": str(path)}
