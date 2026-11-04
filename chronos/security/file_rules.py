"""Threat rules for file edits by coding agents (Edit / Write / MultiEdit, and files a shell command writes).

Path rules flag edits to files that control the agent or the checks around it: Chronos's own state,
agent settings, CI pipelines, git hooks, lockfiles and secret-scanner allowlists. Content rules
compare the old and new text: tests that gain skip markers or lose assertions, hard-coded secrets,
and security settings switched off.

Decisions are `deny`, `ask` or `log` (recorded in the audit log, not shown to the developer), and
`policy.rules` overrides them exactly as for command rules.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from chronos.security.command_rules import TEST_PATH_RE, Hit

# (rule id, category, decision, path regex, why)
PATH_RULES: list[tuple[str, str, str, re.Pattern, str]] = [
    ("integrity.chronos_state", "integrity", "deny", re.compile(r"^\.chronos/"), "is Chronos's own policy, audit and index data"),
    ("integrity.agent_hooks", "integrity", "deny", re.compile(r"^\.claude/settings(?:\.local)?\.json$|^\.codex/(?:config\.toml|requirements\.toml|hooks)"), "controls the agent's hooks, which could switch Chronos off"),
    ("integrity.agent_config", "integrity", "ask", re.compile(r"^\.mcp\.json$|^\.claude/|^\.codex/|^\.gemini/|^\.cursor/|^\.windsurf/|^\.continue/|^\.aider|^\.vscode/(?:settings|tasks|launch)\.json$|^\.idea/workspace\.xml$|^\.devcontainer/"), "changes what tools the agent or editor can run"),
    ("integrity.ci_config", "integrity", "ask", re.compile(r"^\.github/(?:workflows|actions)/|^\.gitlab-ci\.ya?ml$|^\.gitlab/ci/|^\.circleci/|(?:^|/)Jenkinsfile$|^azure-pipelines\.ya?ml$|^bitbucket-pipelines\.yml$|^\.buildkite/|^\.travis\.yml$|^\.drone\.yml$|^cloudbuild\.ya?ml$|^buildspec\.ya?ml$|^\.woodpecker|^\.tekton/|^appveyor\.yml$"), "changes the CI pipeline that checks every change"),
    ("integrity.git_hooks", "integrity", "ask", re.compile(r"^\.husky/|^\.pre-commit-config\.ya?ml$|^\.?lefthook\.ya?ml$|^\.githooks/|^\.lintstagedrc|^lint-staged\.config\.|^\.simple-git-hooks|(?:^|/)CODEOWNERS$"), "changes pre-commit hooks or required reviewers"),
    ("integrity.secret_scanner", "integrity", "ask", re.compile(r"(?:^|/)(?:\.gitleaks\.toml|\.gitleaksignore|\.secrets\.baseline|\.trufflehog[\w.-]*|\.talismanrc|\.semgrepignore|\.snyk|\.trivyignore|\.bandit)$"), "changes a security scanner's allowlist"),
    ("dependencies.lockfile", "dependencies", "ask", re.compile(r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|bun\.lockb?|poetry\.lock|uv\.lock|Pipfile\.lock|pdm\.lock|Cargo\.lock|go\.sum|composer\.lock|Gemfile\.lock|packages\.lock\.json|gradle\.lockfile|mix\.lock|pubspec\.lock|flake\.lock|Package\.resolved)$"), "is a lockfile, which tools should generate; hand edits can point dependencies at other code"),
    ("integrity.check_config", "integrity", "log", re.compile(r"(?:^|/)(?:\.eslintrc[\w.]*|eslint\.config\.[cm]?[jt]s|\.flake8|mypy\.ini|\.mypy\.ini|\.pylintrc|pylintrc|\.?ruff\.toml|pytest\.ini|tox\.ini|setup\.cfg|jest\.config\.[\w.]+|vitest\.config\.[\w.]+|\.golangci\.ya?ml|tsconfig[\w.-]*\.json|\.coveragerc|codecov\.ya?ml|\.codecov\.ya?ml|sonar-project\.properties|\.prettierrc[\w.]*|biome\.json|phpstan\.neon|\.rubocop\.yml|clippy\.toml|\.swiftlint\.yml)$"), "configures linters, type checks, tests or coverage"),
]

SKIP_MARKERS = re.compile(
    r"@pytest\.mark\.(?:skip|xfail)\b|\bpytest\.skip\s*\(|@unittest\.skip|\bself\.skipTest\s*\(|\b(?:it|test|describe|context)\.(?:skip|only|todo)\s*\(|\bx(?:it|describe|test)\s*\("
    r"|\bt\.Skip(?:f|Now)?\s*\(|@Disabled\b|@Ignore\b|#\[ignore\]|\bskip\s*\(\s*['\"]|\.skip\(\s*true|@unittest\.expectedFailure|\bpending\s*\(",
)
ASSERTIONS = re.compile(r"\bassert\w*\b|\bexpect\s*\(|\bshould\b|\bt\.(?:Error|Fatal|Fail)\w*\s*\(|\brequire\.\w+\s*\(|\bassertThat\b|\bverify\s*\(")
TRIVIAL_ASSERT = re.compile(r"\bassert\s+True\b|\bassert\s+1\b|expect\s*\(\s*true\s*\)\s*\.toBe\s*\(\s*true\s*\)|assertTrue\s*\(\s*true\s*\)|\bassert\s+\w+\s+or\s+True\b")
SECRET_VALUE = re.compile(
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP |ENCRYPTED )?PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{60,}"
    r"|\bsk-(?:live|proj|ant(?:-api\d+)?)-[A-Za-z0-9_-]{20,}|\bsk_live_[A-Za-z0-9]{20,}|\brk_live_[A-Za-z0-9]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}|\bAIza[0-9A-Za-z_-]{35}\b"
    r"|\bglpat-[A-Za-z0-9_-]{20}\b|\bnpm_[A-Za-z0-9]{36}\b|\bSG\.[\w-]{22}\.[\w-]{43}\b|\bhf_[A-Za-z0-9]{34,}\b|\bdop_v1_[a-f0-9]{64}\b|\bAccountKey=[A-Za-z0-9+/=]{40,}"
    r"|\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^\s:@/'\"]+:[^\s@/'\"$<{]{6,}@(?!(?:localhost|127\.0\.0\.1|0\.0\.0\.0|db|postgres|mysql|redis|mongo)(?:[:/'\"\s]|$))"
)
SECURITY_WEAKENING = [
    (re.compile(r"\bverify\s*=\s*False\b|ssl\._create_unverified_context|CERT_NONE|check_hostname\s*=\s*False"), "turns off TLS certificate checks"),
    (re.compile(r"rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED\s*[=:]\s*['\"]?0|InsecureSkipVerify\s*:\s*true|curl_setopt\([^)]*SSL_VERIFYPEER[^)]*(?:false|0)"), "turns off TLS certificate checks"),
    (re.compile(r"allow_origins\s*=\s*\[\s*['\"]\*['\"]|Access-Control-Allow-Origin['\"]?\s*[,:]\s*['\"]\*|origin\s*:\s*['\"]\*['\"]|CORS_ORIGIN_ALLOW_ALL\s*=\s*True|AllowAnyOrigin\s*\("), "allows requests from any website (CORS *)"),
    (re.compile(r"@csrf_exempt|csrf\s*[:=]\s*false|WTF_CSRF_ENABLED\s*=\s*False|\.csrf\(\)\.disable\(\)"), "turns off CSRF protection"),
    (re.compile(r"\bAllowAny\b|permitAll\(\)|@PermitAll|skip_before_action\s+:authenticate|\[AllowAnonymous\]"), "removes an authentication requirement"),
    (re.compile(r"^\s*DEBUG\s*=\s*True\b", re.M), "turns on debug mode"),
    (re.compile(r"\bshell\s*=\s*True\b|\bos\.system\s*\(|child_process\.exec\s*\(\s*`"), "runs shell commands built from strings"),
    (re.compile(r"\byaml\.load\s*\((?![^)]*Loader)|\bpickle\.loads?\s*\(|\bmarshal\.loads\s*\(|\beval\s*\(|\bnew\s+Function\s*\("), "runs or deserialises untrusted data"),
    (re.compile(r"verify_signature['\"]?\s*:\s*False|algorithms\s*=\s*\[\s*['\"]none['\"]|\bverify\s*=\s*False\s*\)|jwt\.decode\([^)]*verify\s*=\s*False"), "turns off token signature checks"),
    (re.compile(r"dangerouslySetInnerHTML|\.innerHTML\s*=|\|\s*safe\b|mark_safe\s*\(|v-html\s*="), "inserts unescaped HTML"),
    (re.compile(r"(?:SESSION_COOKIE_SECURE|CSRF_COOKIE_SECURE|SECURE_SSL_REDIRECT)\s*=\s*False|httpOnly\s*:\s*false|secure\s*:\s*false"), "weakens cookie or HTTPS settings"),
    (re.compile(r"0o?777\b|chmod\s*\([^)]*0?777"), "makes files world-writable"),
]
GITIGNORE_SECRET_LINE = re.compile(r"^\s*!?[^#\n]*(?:\.env|\.pem|\.key|secret|credential|\.tfstate|id_rsa)[^\n]*$", re.M | re.I)


def _count(rx: re.Pattern, text: str) -> int:
    return len(rx.findall(text or ""))


def evaluate_path(rel: str) -> list[Hit]:
    rel = PurePosixPath(rel).as_posix().removeprefix("./")
    hits = []
    for rid, cat, decision, rx, why in PATH_RULES:
        if rx.search(rel):
            hits.append(Hit(rid, cat, decision, f"{rel} {why}"))
            if decision == "deny":
                break
    return hits


def evaluate_edit(rel: str, old: str | None, new: str | None) -> list[Hit]:
    """Path rules plus content rules for one edit. `old` is the replaced text (Edit) or the current
    file content (Write); `new` is what replaces it. Either can be None when unknown."""
    hits = evaluate_path(rel)
    if new is None:
        return hits
    old = old or ""
    name = PurePosixPath(rel).name
    if TEST_PATH_RE.search(rel):
        if _count(SKIP_MARKERS, new) > _count(SKIP_MARKERS, old):
            hits.append(Hit("integrity.test_skipped", "integrity", "ask", f"marks tests in {rel} as skipped or focused, so they stop checking the code"))
        removed = _count(ASSERTIONS, old) - _count(ASSERTIONS, new)
        if removed > 0 and old:
            hits.append(Hit("integrity.assertions_removed", "integrity", "ask", f"removes {removed} assertion(s) from {rel}"))
        if _count(TRIVIAL_ASSERT, new) > _count(TRIVIAL_ASSERT, old):
            hits.append(Hit("integrity.trivial_assert", "integrity", "ask", f"adds an assertion in {rel} that always passes"))
    secrets_new = set(SECRET_VALUE.findall(new)) - set(SECRET_VALUE.findall(old))
    if secrets_new:
        sample = next(iter(secrets_new))
        hits.append(Hit("secrets.hardcoded", "secrets", "ask", f"writes what looks like a real credential into {rel} ({sample[:8]}...)"))
    for rx, why in SECURITY_WEAKENING:
        if _count(rx, new) > _count(rx, old):
            hits.append(Hit("integrity.security_weakened", "integrity", "log", f"change to {rel} {why}"))
    if name == ".gitignore" and old:
        lost = set(m.strip() for m in GITIGNORE_SECRET_LINE.findall(old)) - set(m.strip() for m in GITIGNORE_SECRET_LINE.findall(new))
        lost = {x for x in lost if not x.startswith("!")}
        if lost:
            hits.append(Hit("secrets.gitignore_unignored", "secrets", "ask", f".gitignore stops ignoring {', '.join(sorted(lost)[:3])}, so secrets could be committed"))
    return hits
