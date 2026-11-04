"""Threat rules for shell commands run by coding agents.

`chronos.security.shell.analyze` turns a command line into simple commands; the rules here decide
which of them need the developer's approval (`ask`), are refused (`deny`) or are only recorded
(`log`). Rules come from documented agent incidents (deleted home directories and production
databases, force pushes, leaked tokens) and from the OWASP agentic-AI risks (tool misuse, data
exfiltration, supply chain).

Every rule has an id (`git.push_force`) and a category (`git`). A repo can change any decision in
`.chronos/config.json` -> `policy.rules`, keyed by rule id, category or `*`:

    {"policy": {"rules": {"dependencies": "log", "git.push_protected": "deny", "production": "off"}}}

Static checks of command text cannot be complete: a program can do anything once it runs. The rules
cover the common paths an agent takes by mistake or under prompt injection; the PostToolUse backstop
and the audit log cover the rest.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from chronos.security.shell import (
    INTERPRETERS,
    SHELLS,
    ShellAnalysis,
    SimpleCmd,
    _expand,
    _non_options,
    find_secret_names,
)

DECISIONS = ("deny", "ask", "log", "off")
CATEGORIES = {
    "destructive": "Deletes or overwrites data",
    "git": "Rewrites or discards git history or work",
    "exfiltration": "Sends data off the machine",
    "secrets": "Reads credentials or tokens",
    "production": "Changes live infrastructure, deployments or databases",
    "dependencies": "Adds code from outside the project",
    "release": "Publishes packages or images",
    "machine": "Changes the developer's machine",
    "integrity": "Weakens tests, CI or other checks",
    "hidden": "Hides what will actually run",
    "mcp": "Risky action through an MCP tool",
}


@dataclass(frozen=True)
class Hit:
    rule: str
    category: str
    decision: str  # deny | ask | log
    reason: str


@dataclass
class Ctx:
    root: Path
    raw: str
    _branch: str | None = None
    _git_cache: dict | None = None

    def branch(self) -> str:
        if self._branch is None:
            try:
                head = (self.root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
                self._branch = head.rsplit("/", 1)[-1] if head.startswith("ref:") else ""
            except OSError:
                self._branch = ""
        return self._branch

    def git(self, *args: str) -> str:
        self._git_cache = self._git_cache or {}
        if args not in self._git_cache:
            try:
                r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=3)
                self._git_cache[args] = r.stdout if r.returncode == 0 else ""
            except Exception:  # noqa: BLE001
                self._git_cache[args] = ""
        return self._git_cache[args]


RuleFn = Callable[[SimpleCmd, Ctx], "Iterable[Hit] | Hit | None"]
_CMD_RULES: list[RuleFn] = []


def cmd_rule(fn: RuleFn) -> RuleFn:
    _CMD_RULES.append(fn)
    return fn


def _flags(args: list[str]) -> set[str]:
    """Short flags split (`-rf` -> {-r, -f}) plus long flags (without `=value`)."""
    out: set[str] = set()
    for a in args:
        if a.startswith("--"):
            out.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1 and not a[1].isdigit():
            out.update(f"-{c}" for c in a[1:])
    return out


def _sub(args: list[str], n: int = 0) -> str:
    opts = _non_options(args)
    return opts[n] if len(opts) > n else ""


def _skip_valued(args: list[str], valued: set[str]) -> list[str]:
    """Non-option arguments, also skipping the values of options that take one (`--context prod`)."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a in valued:
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _home() -> Path:
    return Path.home().resolve()


SYSTEM_DIRS = {"/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib64", "/opt", "/proc", "/root", "/sbin",
               "/srv", "/sys", "/usr", "/var", "/Users", "/System", "/Library", "/Applications", "/Volumes", "/private"}
BUILD_DIRS = {"node_modules", "dist", "build", "out", "target", "__pycache__", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".next", ".nuxt", ".turbo", ".cache", "coverage", "htmlcov", ".tox", ".nox", ".parcel-cache",
              ".gradle", "bin", "obj", ".eggs", "tmp", ".svelte-kit", ".angular", ".expo", "vendor/bundle"}
PROTECTED_BRANCHES_RE = re.compile(r"^(?:main|master|production|prod|release(?:[/-].*)?|trunk|live|stable)$")
TEST_PATH_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|specs|testing)(?:/|$)|(?:^|/)test_[^/]+\.py$|_test\.(?:py|go)$|\.(?:test|spec)\.[cm]?[jt]sx?$|Test\.java$|_spec\.rb$")


# ----------------------------------------------------------------------------- destructive
def _danger_target(word: str, cwd: Path, root: Path) -> str | None:
    """Why deleting/chmodding `word` recursively would be catastrophic, or None."""
    raw = word.strip()
    if raw in ("/", "/*", "/.", "~", "~/", "~/*", "$HOME", "${HOME}", "$HOME/", "${HOME}/", "$HOME/*", "..", "../", "../*", "/..", "*/"):
        return f"'{raw}' is the filesystem root, your home directory or a parent folder"
    expanded = os.path.expanduser(os.path.expandvars(raw))
    if "$" in expanded:
        return f"'{raw}' depends on a variable Chronos cannot see; if it is empty the command deletes from '/'"
    p = Path(expanded)
    p = (p if p.is_absolute() else cwd / p)
    try:
        rp = p.resolve()
    except (OSError, RuntimeError):
        return None
    if str(rp) in SYSTEM_DIRS or rp == _home() or str(rp).rstrip("/") in SYSTEM_DIRS:
        return f"'{raw}' resolves to {rp}, a system or home directory"
    if rp == root or root.is_relative_to(rp):
        return f"'{raw}' resolves to {rp}, which contains the whole repository"
    if rp.name == ".git" and rp.parent == root:
        return f"'{raw}' is the repository's git history"
    return None


@cmd_rule
def rm_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    if prog not in ("rm", "rmdir", "shred", "unlink", "srm", "trash", "trash-put"):
        return None
    flags = _flags(args)
    recursive = bool(flags & {"-r", "-R", "--recursive"}) or prog == "rmdir"
    if "--no-preserve-root" in flags:
        return Hit("destructive.rm_root", "destructive", "deny", "rm --no-preserve-root can delete the whole filesystem")
    targets = _non_options(args)
    if "find-exec" in c.prefixes or "xargs" in c.prefixes:
        return Hit("destructive.rm_computed", "destructive", "ask", f"{prog} deletes files chosen at runtime (find/xargs), so Chronos can't check which")
    hits = []
    for t in targets:
        why = _danger_target(t, c.cwd, ctx.root)
        if why and (recursive or "*" in t or why.startswith(f"'{t}' depends")):
            hits.append(Hit("destructive.rm_catastrophic", "destructive", "deny", f"{prog} would delete too much: {why}"))
    if hits:
        return hits
    # Recursive deletes of tracked code, and deletes that throw away uncommitted work.
    inside = []
    for t in targets:
        for p in _expand(t, c.cwd):
            try:
                rel = p.resolve().relative_to(ctx.root)
            except (ValueError, OSError):
                continue
            if rel.parts and rel.parts[0] in BUILD_DIRS or any(part in BUILD_DIRS for part in rel.parts[:1]):
                continue
            inside.append(rel.as_posix() or ".")
    if not inside:
        return None
    tracked = ctx.git("ls-files", "-z", "--", *inside[:50]).split("\0")
    tracked = [t for t in tracked if t]
    dirty = [ln[3:] for ln in ctx.git("status", "--porcelain", "--", *inside[:50]).splitlines() if ln[:2].strip() and not ln.startswith("??")]
    out = []
    tests = [t for t in tracked if TEST_PATH_RE.search(t)]
    if tests:
        out.append(Hit("integrity.delete_tests", "integrity", "ask", f"deletes {len(tests)} test file(s), e.g. {tests[0]}"))
    if dirty:
        out.append(Hit("destructive.rm_uncommitted", "destructive", "ask", f"deletes file(s) with uncommitted changes that git can't restore: {', '.join(dirty[:3])}"))
    elif recursive and len(tracked) > 1:
        out.append(Hit("destructive.rm_tracked_dir", "destructive", "ask", f"recursively deletes {len(tracked)} tracked file(s) under {', '.join(inside[:2])}"))
    return out


@cmd_rule
def find_delete(c: SimpleCmd, ctx: Ctx):
    if c.prog == "find" and "-delete" in c.args:
        return Hit("destructive.find_delete", "destructive", "ask", "find -delete removes every matching file")
    return None


DISK_TOOLS = {"mkfs", "wipefs", "fdisk", "sfdisk", "gdisk", "sgdisk", "parted", "mke2fs", "mkswap", "blkdiscard", "hdparm"}


@cmd_rule
def disk_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    if prog in DISK_TOOLS or prog.startswith("mkfs."):
        return Hit("destructive.disk", "destructive", "deny", f"{prog} can erase a disk or partition")
    if prog == "dd" and any(re.match(r"of=/dev/(?!null|zero|stdout|stderr)", a) for a in args):
        return Hit("destructive.disk", "destructive", "deny", "dd writes directly to a device")
    if prog == "diskutil" and any(a.lower().startswith(("erase", "partition", "zero", "secureerase", "apfs")) for a in args):
        return Hit("destructive.disk", "destructive", "deny", "diskutil can erase a disk")
    if prog in ("format", "format.com", "Format-Volume", "Clear-Disk", "Initialize-Disk") or (prog == "cipher" and any(a.lower().startswith("/w") for a in args)):
        return Hit("destructive.disk", "destructive", "deny", f"{prog} can erase a disk")
    return None


@cmd_rule
def power_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    if prog in ("shutdown", "reboot", "halt", "poweroff") or (prog == "init" and _sub(args) in ("0", "6")) or (prog == "systemctl" and _sub(args) in ("poweroff", "reboot", "halt", "kexec", "suspend", "hibernate")):
        return Hit("destructive.power", "destructive", "deny", f"{prog} shuts down or restarts the machine")
    if prog == "kill" and ("-1" in _non_options(args) or any(a == "-1" for a in args[1:])):
        return Hit("destructive.kill_all", "destructive", "deny", "kill -1 stops every process you own")
    if prog in ("killall", "pkill", "killall5", "Stop-Process", "taskkill"):
        return Hit("destructive.kill_many", "destructive", "ask", f"{prog} stops processes by name, possibly ones the agent didn't start")
    return None


@cmd_rule
def perm_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    if prog not in ("chmod", "chown", "chgrp", "setfacl", "icacls", "takeown"):
        return None
    out = []
    if _flags(args) & {"-R", "--recursive"}:
        for t in _non_options(args)[1:]:
            why = _danger_target(t, c.cwd, ctx.root)
            if why:
                out.append(Hit("destructive.chmod_catastrophic", "destructive", "deny", f"{prog} -R on {why}"))
    if prog == "chmod":
        mode = _sub(args)
        if re.fullmatch(r"0?[0-7]?777|a\+rwx|ugo\+rwx|o\+w|a\+w|\+w", mode) or re.fullmatch(r"[ugoa]*\+[rwx]*s[rwx]*|[2467][0-7]{3}", mode):
            out.append(Hit("machine.chmod_open", "machine", "ask", f"chmod {mode} makes files world-writable or setuid"))
    return out


@cmd_rule
def mv_rules(c: SimpleCmd, ctx: Ctx):
    if c.prog == "mv" and _non_options(c.args)[-1:] == ["/dev/null"]:
        return Hit("destructive.mv_devnull", "destructive", "ask", "mv to /dev/null destroys the file")
    if c.prog in ("rd", "rmdir", "del", "erase", "Remove-Item", "ri") and any(a.lower() in ("/s", "/q", "-recurse", "-force") for a in c.args):
        return Hit("destructive.windows_delete", "destructive", "ask", f"{c.prog} deletes recursively without confirmation")
    return None


# ----------------------------------------------------------------------------- git
def _git_parts(c: SimpleCmd) -> tuple[str, list[str], list[str]]:
    """(subcommand, args after it, global -c settings)."""
    args = c.args
    configs, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-c", "-C", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
            if args[i] == "-c":
                configs.append(args[i + 1])
            i += 2
            continue
        i += 1
    if i >= len(args):
        return "", [], configs
    return args[i], args[i + 1:], configs


@cmd_rule
def git_rules(c: SimpleCmd, ctx: Ctx):
    if c.prog != "git":
        return None
    sub, rest, configs = _git_parts(c)
    flags = _flags(rest)
    pos = _non_options(rest)
    out: list[Hit] = []
    if any(cfg.lower().startswith(("core.hookspath", "core.sshcommand", "core.fsmonitor", "core.pager", "alias.")) for cfg in configs):
        out.append(Hit("integrity.git_hooks_bypass", "integrity", "ask", f"git -c {configs[0]} changes how git runs hooks or commands"))
    if sub == "push":
        if flags & {"-f", "--force", "--force-with-lease", "--force-if-includes"} or any(p.startswith("+") for p in pos[1:]):
            out.append(Hit("git.push_force", "git", "ask", "force-push overwrites history on the remote"))
        if flags & {"--delete", "-d", "--mirror", "--prune"} or any(p.startswith(":") for p in pos[1:]):
            out.append(Hit("git.push_delete", "git", "ask", "push deletes or mirrors remote branches"))
        if "--no-verify" in flags:
            out.append(Hit("integrity.no_verify", "integrity", "ask", "push --no-verify skips the repository's pre-push checks"))
        if pos and re.match(r"^(?:https?://|git@|ssh://|[\w.-]+@[\w.-]+:)", pos[0]):
            out.append(Hit("exfiltration.push_url", "exfiltration", "ask", f"pushes to an address that isn't a configured remote: {pos[0]}"))
        targets = [p.split(":", 1)[-1].lstrip("+").removeprefix("refs/heads/") for p in pos[1:]]
        if not targets and ctx.branch():
            targets = [ctx.branch()]
        prot = [t for t in targets if PROTECTED_BRANCHES_RE.match(t) or (t == "HEAD" and PROTECTED_BRANCHES_RE.match(ctx.branch() or "-"))]
        if prot:
            out.append(Hit("git.push_protected", "git", "ask", f"pushes directly to the protected branch '{prot[0] if prot[0] != 'HEAD' else ctx.branch()}'"))
    elif sub == "reset" and "--hard" in flags:
        out.append(Hit("git.reset_hard", "git", "ask", "git reset --hard discards uncommitted changes"))
    elif sub == "clean" and (flags & {"-f", "--force"}) and not (flags & {"-n", "--dry-run"}):
        out.append(Hit("git.clean", "git", "ask", "git clean -f permanently deletes untracked files" + (" including ignored ones (.env, local config)" if "-x" in flags or "-X" in flags else "")))
    elif sub in ("checkout", "restore", "switch"):
        broad = {".", "./", ":/", "*", ":(top)"}
        if (flags & {"-f", "--force", "--discard-changes"}) or (sub != "switch" and any(p in broad for p in pos)) or (sub == "checkout" and "--" in rest and any(p in broad for p in rest[rest.index("--") + 1:])):
            out.append(Hit("git.discard_changes", "git", "ask", f"git {sub} discards uncommitted changes across the working tree"))
    elif sub == "stash" and pos[:1] and pos[0] in ("drop", "clear"):
        out.append(Hit("git.stash_drop", "git", "ask", f"git stash {pos[0]} deletes saved work"))
    elif sub == "branch" and (flags & {"-D"} or ({"-d", "--delete"} <= flags | {"--delete"} and "--force" in flags)):
        out.append(Hit("git.branch_force_delete", "git", "ask", "git branch -D deletes a branch even if it isn't merged"))
    elif sub in ("filter-branch", "filter-repo", "replace") or (sub == "update-ref" and "-d" in flags) or (sub == "reflog" and pos[:1] == ["expire"]) or (sub in ("gc", "prune") and any(a.startswith("--prune=now") or a == "--aggressive" for a in rest)):
        out.append(Hit("git.history_rewrite", "git", "ask", f"git {sub} rewrites or prunes history"))
    elif sub == "remote" and pos[:1] and pos[0] in ("add", "set-url", "rename", "remove", "rm"):
        out.append(Hit("exfiltration.git_remote", "exfiltration", "ask", f"git remote {pos[0]} changes where code is pushed"))
    elif sub == "config" and flags & {"--global", "--system"}:
        out.append(Hit("machine.git_config_global", "machine", "ask", "changes git configuration for every repository on this machine"))
    elif sub == "add" and (flags & {"-A", "--all", "-f", "--force"} or any(p in (".", "*", ":/") for p in pos)):
        untracked = [ln for ln in ctx.git("ls-files", "--others", "--exclude-standard").splitlines() if find_secret_names(Path(ln).name)]
        forced = [p for p in pos if find_secret_names(Path(p).name)] if flags & {"-f", "--force"} else []
        if untracked or forced:
            out.append(Hit("secrets.git_add_secret", "secrets", "deny", f"would stage secret file(s) for commit: {', '.join((untracked or forced)[:3])}"))
    if sub in ("commit", "merge", "rebase", "cherry-pick", "am", "revert") and ("--no-verify" in flags or (sub == "commit" and "-n" in flags)):
        out.append(Hit("integrity.no_verify", "integrity", "ask", f"git {sub} --no-verify skips the repository's pre-commit checks"))
    if sub == "credential" and pos[:1] and pos[0] in ("fill", "get"):
        out.append(Hit("secrets.token_command", "secrets", "deny", "git credential fill prints stored credentials"))
    return out


@cmd_rule
def gh_rules(c: SimpleCmd, ctx: Ctx):
    if c.prog not in ("gh", "glab"):
        return None
    pos = _non_options(c.args)
    a = " ".join(pos[:3])
    flags = _flags(c.args)
    if re.match(r"^repo (?:delete|archive)", a):
        return Hit("git.repo_delete", "git", "deny", f"{c.prog} {a} deletes or archives the repository")
    if a.startswith("auth token") or (a.startswith("auth status") and flags & {"-t", "--show-token"}) or a.startswith("auth refresh"):
        return Hit("secrets.token_command", "secrets", "deny", f"{c.prog} {a} prints your GitHub token")
    if (a.startswith("repo edit") and any("visibility" in x for x in c.args)) or (a.startswith("repo create") and "--public" in flags):
        return Hit("exfiltration.repo_public", "exfiltration", "ask", "makes a repository public")
    if a.startswith("gist create") or a.startswith("snippet create"):
        return Hit("exfiltration.gist", "exfiltration", "ask", "uploads files to a gist or snippet")
    if re.match(r"^(?:pr|mr) merge", a):
        return Hit("git.pr_merge", "git", "ask", "merges a pull request without a human review step")
    if re.match(r"^release (?:create|delete|upload|edit)", a):
        return Hit("release.gh_release", "release", "ask", f"{c.prog} {a} publishes or deletes a release")
    if re.match(r"^(?:secret|variable) (?:set|delete|remove)", a):
        return Hit("production.gh_secret", "production", "ask", f"{c.prog} {a} changes CI secrets or variables")
    if pos[:1] == ["api"] and any(re.match(r"^(?:-X|--method)=?(?:POST|PUT|PATCH|DELETE)?$", x, re.I) for x in c.args) and any(x.upper() in ("POST", "PUT", "PATCH", "DELETE") or re.search(r"=(?:POST|PUT|PATCH|DELETE)$", x, re.I) or x.upper().endswith(("POST", "PUT", "PATCH", "DELETE")) for x in c.args):
        return Hit("git.gh_api_write", "git", "ask", "gh api call that changes data on GitHub")
    if re.match(r"^workflow (?:run|disable|enable)", a) or re.match(r"^run (?:cancel|rerun|delete)", a):
        return Hit("integrity.ci_control", "integrity", "ask", f"{c.prog} {a} controls CI runs")
    return None


# ----------------------------------------------------------------------------- exfiltration
EXFIL_HOSTS_RE = re.compile(
    r"\b(?:pastebin\.com|paste\.ee|hastebin\.com|hastebin\.skyra\.pw|0x0\.st|transfer\.sh|termbin\.com|ix\.io|sprunge\.us"
    r"|file\.io|tmpfiles\.org|catbox\.moe|litterbox\.catbox\.moe|webhook\.site|requestbin\.(?:com|net)|pipedream\.net|"
    r"[\w-]+\.m\.pipedream\.net|ngrok(?:-free)?\.(?:io|app|dev)|serveo\.net|localhost\.run|trycloudflare\.com|"
    r"interact\.sh|oast\.(?:fun|live|me|online|pro|site)|burpcollaborator\.net|dnslog\.cn|canarytokens\.com|"
    r"dpaste\.(?:com|org)|rentry\.co|ghostbin\.\w+|privatebin\.net|beeceptor\.com|hookbin\.com|postb\.in|"
    r"discord(?:app)?\.com/api/webhooks|hooks\.slack\.com|api\.telegram\.org/bot)",
    re.I,
)
CURL_DATA_FLAGS = {"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "--data-ascii", "-F", "--form",
                   "--form-string", "-T", "--upload-file", "--json"}


def _curl_method(args: list[str]) -> str:
    for i, a in enumerate(args):
        if a in ("-X", "--request") and i + 1 < len(args):
            return args[i + 1].upper()
        m = re.match(r"^(?:-X|--request=)([A-Za-z]+)$", a)
        if m:
            return m.group(1).upper()
    return ""


@cmd_rule
def network_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    out = []
    joined = " ".join(c.words)
    if EXFIL_HOSTS_RE.search(joined):
        out.append(Hit("exfiltration.paste_host", "exfiltration", "deny", "contacts a paste, webhook or tunnel service commonly used to leak data"))
    if prog == "curl":
        data = [a for a in args if a.split("=", 1)[0] in CURL_DATA_FLAGS or re.match(r"^-[dFT].+", a)]
        method = _curl_method(args)
        files = [x.split("@", 1)[1] for x in args if "@" in x and re.match(r"^(?:[\w-]+=)?@", x)]
        if data or method in ("POST", "PUT", "PATCH", "DELETE"):
            what = f" (uploads {', '.join(files[:2])})" if files else ""
            out.append(Hit("exfiltration.http_upload", "exfiltration", "ask", f"curl sends data to a server{what}"))
    elif prog in ("wget", "wget2"):
        if any(a.startswith(("--post-data", "--post-file", "--body-data", "--body-file")) or re.match(r"^--method=(?:POST|PUT|PATCH|DELETE)", a, re.I) for a in args):
            out.append(Hit("exfiltration.http_upload", "exfiltration", "ask", "wget sends data to a server"))
    elif prog in ("http", "https", "xh", "xhs", "httpie"):
        pos = _non_options(args)
        if (pos and pos[0].upper() in ("POST", "PUT", "PATCH", "DELETE")) or any(re.match(r"^[\w.-]+(?:=|:=|@)", p) for p in pos[1:]):
            out.append(Hit("exfiltration.http_upload", "exfiltration", "ask", f"{prog} sends data to a server"))
    elif prog in ("Invoke-WebRequest", "iwr", "Invoke-RestMethod", "irm"):
        if any(a.lower() in ("-body", "-infile") for a in args) or any(a.lower() == "-method" for a in args):
            out.append(Hit("exfiltration.http_upload", "exfiltration", "ask", f"{prog} sends data to a server"))
    if prog in ("nc", "ncat", "netcat", "socat", "telnet", "cryptcat", "pwncat"):
        out.append(Hit("exfiltration.raw_socket", "exfiltration", "ask", f"{prog} opens a raw network connection"))
    if prog in ("dig", "nslookup", "host", "drill", "ping", "ping6", "traceroute", "resolvectl") and any("__CHRONOS_SUBST__" in a or "$" in a for a in args):
        out.append(Hit("exfiltration.dns", "exfiltration", "ask", f"{prog} with a computed host name can leak data through DNS"))
    if prog in ("scp", "sftp", "rsync", "rclone", "croc", "magic-wormhole", "wormhole", "ffsend", "aws-s3-sync"):
        remote = [a for a in _non_options(args) if re.match(r"^(?:[\w.-]+@)?[\w-]+(?:\.[\w-]+)*:(?!//)", a) or a.startswith(("rsync://", "s3:", "gs:", "remote:")) or prog in ("croc", "wormhole", "magic-wormhole", "ffsend")]
        if remote or (prog == "rclone" and _sub(args) in ("copy", "sync", "move", "copyto", "moveto", "rcat")):
            out.append(Hit("exfiltration.remote_copy", "exfiltration", "ask", f"{prog} copies files to another machine"))
    if prog in ("mail", "mailx", "sendmail", "mutt", "msmtp", "swaks"):
        out.append(Hit("exfiltration.mail", "exfiltration", "ask", f"{prog} sends email"))
    if prog in ("ssh", "mosh", "rsh", "plink"):
        if _non_options(args):
            out.append(Hit("production.remote_shell", "production", "ask", f"{prog} runs commands on another machine"))
    return out


# ----------------------------------------------------------------------------- secrets
SECRET_VAR_RE = re.compile(r"\$\{?(\w*(?:KEY|TOKEN|SECRET|PASS(?:WORD|WD)?|PWD|CREDENTIAL|AUTH|PRIVATE|COOKIE|SESSION)\w*)\}?", re.I)


@cmd_rule
def secret_cmd_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    pos = _non_options(args)
    a = " ".join(pos[:4])
    flags = _flags(args)
    deny = lambda why: Hit("secrets.token_command", "secrets", "deny", why)  # noqa: E731
    ask = lambda why: Hit("secrets.credential_store", "secrets", "ask", why)  # noqa: E731
    if prog == "gcloud" and re.search(r"\bauth\b.*\bprint-(?:access|identity)-token\b|\bconfig config-helper\b", " ".join(args)):
        return deny("gcloud prints an access token")
    if prog == "aws" and (re.match(r"^configure (?:get|export-credentials)", a) or re.match(r"^(?:sts get-session-token|ecr get-login-password|ecr-public get-login-password|codeartifact get-authorization-token|secretsmanager get-secret-value|ssm get-parameters?)", a)):
        return deny(f"aws {a} prints credentials or secret values")
    if prog == "az" and re.match(r"^account get-access-token|^keyvault secret show", a):
        return deny(f"az {a} prints a token or secret")
    if prog in ("kubectl", "oc") and (("config" in pos and "view" in pos and flags & {"--raw", "--flatten"}) or (pos[:1] in (["get"], ["describe"]) and len(pos) > 1 and pos[1].startswith("secret") and (flags & {"-o", "--output"} or pos[0] == "describe" or any(x.startswith(("-o", "--output")) for x in args)))):
        return deny(f"{prog} prints cluster credentials or secret values")
    if prog == "security" and pos[:1] and pos[0] in ("find-generic-password", "find-internet-password", "dump-keychain", "export"):
        return deny("security reads the macOS keychain")
    if prog == "heroku" and pos[:1] and pos[0] in ("auth:token", "authorizations:create"):
        return deny("heroku prints an API token")
    if (prog == "npm" and a.startswith("token")) or (prog in ("vault",) and pos[:1] and pos[0] in ("read", "kv", "token")) or (prog == "op" and pos[:1] and pos[0] in ("read", "item", "inject", "run")) or (prog == "doppler" and pos[:1] and pos[0] in ("secrets", "run")) or (prog == "pass" and (not pos or pos[0] == "show")) or (prog in ("bw", "lpass") and pos[:1] and pos[0] in ("get", "show", "export")) or (prog in ("sops", "ansible-vault") and pos[:1] and pos[0] in ("-d", "decrypt", "view", "exec-env")) or (prog == "sops" and "-d" in args) or (prog == "gpg" and flags & {"-d", "--decrypt"}):
        return ask(f"{prog} {a}".strip() + " reads a password manager or encrypted secrets")
    if prog in ("docker", "podman", "helm") and pos[:1] == ["login"]:
        return ask(f"{prog} login handles registry credentials")
    if (prog == "printenv" and (not pos or any(SECRET_VAR_RE.fullmatch("$" + p) for p in pos))) or (prog in ("env",) and not pos) or (prog == "export" and "-p" in args) or (prog == "declare" and flags & {"-x", "-p"}) or (prog == "set" and not args) or (prog in ("Get-ChildItem", "gci", "dir", "ls") and any(x.lower().startswith("env:") for x in args)):
        return ask("prints environment variables, which often hold API keys and tokens")
    if prog in ("echo", "printf") and SECRET_VAR_RE.search(" ".join(args)):
        return ask(f"prints {SECRET_VAR_RE.search(' '.join(args)).group(1)}, which looks like a secret")
    return None


RECURSIVE_READERS = {"grep": {"-r", "-R", "--recursive", "--dereference-recursive"}, "egrep": {"-r", "-R"}, "fgrep": {"-r", "-R"},
                     "rgrep": set(), "ag": {"-u", "--unrestricted", "--hidden"}, "rg": {"-u", "--no-ignore", "--hidden", "-.", "--unrestricted"},
                     "ack": set(), "zip": {"-r", "--recurse-paths"}, "7z": set(), "tar": set(), "cp": {"-r", "-R", "-a", "--recursive", "--archive"},
                     "rsync": {"-r", "-a", "--recursive", "--archive"}, "scp": {"-r"}}
_SKIP_WALK = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target", ".tox", ".mypy_cache", ".pytest_cache", ".next", ".cache"}
_walk_cache: dict[str, tuple[float, list[str]]] = {}


def secret_files_under(d: Path, limit_entries: int = 20000, budget_s: float = 0.4) -> list[str]:
    """Secret files under a directory (cached a few seconds), skipping dependency/build folders."""
    key = str(d)
    hit = _walk_cache.get(key)
    if hit and time.monotonic() - hit[0] < 10:
        return hit[1]
    found: list[str] = []
    start, seen = time.monotonic(), 0
    stack = [d]
    while stack and seen < limit_entries and time.monotonic() - start < budget_s:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in _SKIP_WALK:
                            stack.append(Path(e.path))
                    elif find_secret_names(e.name):
                        found.append(os.path.relpath(e.path, d))
        except OSError:
            continue
    _walk_cache[key] = (time.monotonic(), found)
    return found


@cmd_rule
def recursive_read_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    if prog not in RECURSIVE_READERS:
        return None
    needed = RECURSIVE_READERS[prog]
    flags = _flags(args)
    if prog == "tar" and not (flags & {"-c", "--create"} or (args and re.match(r"^[a-zA-Z]*c", args[0]))):
        return None
    if prog == "7z" and _sub(args) not in ("a", "u"):
        return None
    if needed and not (flags & needed) and not (prog == "rg" and "-uu" in args):
        return None
    if prog == "rg" and flags & {"-u"} and "-uu" not in args and "--no-ignore" not in flags and "--hidden" not in flags:
        return None  # rg -u alone still skips hidden files like .env
    if any(("env" in a or "secret" in a) and a.startswith(("--exclude", "--glob=!", "-g!", "--ignore", "-x", "--exclude-from")) for a in args) or any(a.startswith("!") for a in args):
        return None
    pos = _non_options(args)
    if prog in ("grep", "egrep", "fgrep", "rgrep", "ag", "rg", "ack"):
        pattern_given = any(a in ("-e", "-f", "--regexp", "--file") or a.startswith(("-e", "--regexp=")) for a in args)
        pos = pos if pattern_given else pos[1:]
        pos = pos or ["."]
    dirs = [p for t in pos for p in _expand(t, c.cwd) if p.is_dir()]
    found = [f for d in dirs[:5] for f in secret_files_under(d)]
    if found:
        return Hit("secrets.recursive_read", "secrets", "ask", f"{prog} reads every file under {pos[0]}, including secret file(s) {', '.join(found[:3])}. Add an exclude (e.g. --exclude=.env) to avoid this")
    return None


# ----------------------------------------------------------------------------- production
KUBE_MUTATING = {"apply", "create", "delete", "replace", "patch", "edit", "scale", "rollout", "drain", "cordon", "uncordon",
                 "taint", "label", "annotate", "set", "exec", "cp", "run", "expose", "autoscale", "debug", "attach", "certificate"}
CLOUD_MUTATING = {"delete", "deploy", "create", "update", "remove", "destroy", "set-iam-policy", "add-iam-policy-binding",
                  "remove-iam-policy-binding", "restore", "import", "reset", "stop", "start", "resize", "patch", "rollback",
                  "scale", "purge", "drop", "apply", "upgrade", "restart", "cancel", "disable", "enable", "promote", "failover", "suspend", "resume"}
AWS_MUTATING_RE = re.compile(r"^(?:delete|terminate|put|create|update|modify|remove|stop|start|reboot|run|invoke|attach|detach|revoke|authorize|deregister|register|restore|reset|disable|enable|cancel|purge|import|copy|replicate|send|publish|execute|deploy|tag|untag|associate|disassociate|batch-write|batch-delete|rb|rm|mv|sync|mb)\b")
PAAS = {
    "vercel": r"^(?:$|deploy|--prod|rm|remove|promote|rollback|env (?:add|rm|pull)|domains rm|alias|redeploy)",
    "netlify": r"^(?:deploy|sites:delete|env:set|env:unset|api|functions:invoke)",
    "fly": r"^(?:deploy|destroy|apps destroy|scale|secrets|postgres|pg|volumes destroy|machine (?:destroy|stop|kill|restart)|ssh|launch)",
    "flyctl": r"^(?:deploy|destroy|apps destroy|scale|secrets|postgres|pg|volumes destroy|machine (?:destroy|stop|kill|restart)|ssh|launch)",
    "heroku": r"^(?:destroy|apps:destroy|pg:reset|pg:push|pg:copy|run|releases:rollback|config:set|config:unset|ps:scale|maintenance|addons:destroy|ps:restart|ps:kill)",
    "railway": r"^(?:up|down|delete|redeploy|variables|run|connect)",
    "firebase": r"^(?:deploy|database:remove|database:set|database:update|firestore:delete|hosting:disable|functions:delete|projects:delete|auth:import)",
    "wrangler": r"^(?:deploy|publish|delete|rollback|secret|d1 (?:execute|delete|migrations apply)|kv(?::| )(?:key|namespace) delete|r2 (?:object|bucket) delete|pages deploy)",
    "supabase": r"^(?:db (?:reset|push|dump)|projects delete|functions (?:deploy|delete)|secrets (?:set|unset)|migration (?:repair|squash|down)|branches delete|stop --no-backup|link)",
    "pscale": r"^(?:deploy-request deploy|database delete|branch delete|password create|shell)",
    "neonctl": r"^(?:projects delete|branches (?:delete|reset|restore)|databases delete|roles delete)",
    "doctl": r"\b(?:delete|create|update|deploy|destroy|resize|power-off|reboot)\b",
    "hcloud": r"\b(?:delete|create|update|reset|shutdown|poweroff|rebuild)\b",
    "eksctl": r"^(?:create|delete|upgrade|scale|drain|set|unset)",
    "skaffold": r"^(?:run|deploy|delete|apply)",
    "argocd": r"^app (?:sync|delete|rollback|set|patch|terminate-op)",
    "cdk": r"^(?:deploy|destroy|bootstrap)",
    "sam": r"^(?:deploy|delete|sync)",
    "serverless": r"^(?:deploy|remove|rollback)",
    "sls": r"^(?:deploy|remove|rollback)",
    "eb": r"^(?:deploy|terminate|scale|swap|restore|setenv)",
    "copilot": r"\b(?:deploy|delete)\b",
    "amplify": r"^(?:push|publish|delete|remove)",
    "pulumi": r"^(?:up|update|destroy|refresh|import|stack rm|cancel|state delete)",
    "ansible-playbook": r".",
    "ansible": r".",
    "nomad": r"^(?:job (?:run|stop|revert|dispatch)|node drain|system gc)",
    "terraform": r"^(?:apply|destroy|import|taint|untaint|force-unlock|state (?:rm|mv|push|replace-provider)|workspace delete)",
    "tofu": r"^(?:apply|destroy|import|taint|untaint|force-unlock|state (?:rm|mv|push|replace-provider)|workspace delete)",
    "terragrunt": r"^(?:apply|destroy|import|run-all (?:apply|destroy)|state rm)",
    "helm": r"^(?:install|upgrade|uninstall|delete|rollback)",
    "stripe": r"\b(?:post|delete|refunds create|charges create|payouts create|transfers create|subscriptions cancel|customers delete|payment_intents (?:create|confirm|capture))\b",
}
DESTRUCTIVE_SQL_RE = re.compile(
    r"\bdrop\s+(?:database|schema|table|collection|index|view|user|role)\b|\btruncate\s+(?:table\s+)?\w|"
    r"\bdelete\s+from\s+[\w.\"`\[\]]+\s*(?:;|$|\"|')|\balter\s+table\s+\S+\s+drop\b|\bdropDatabase\s*\(|\.drop\s*\(\s*\)|"
    r"\bdeleteMany\s*\(\s*\{\s*\}\s*\)|\bflushall\b|\bflushdb\b|\bupdate\s+[\w.\"`]+\s+set\s+[^;]*?(?:;|$)(?![^;]*\bwhere\b)",
    re.I | re.M,
)
DB_CLIENTS = {"psql", "mysql", "mariadb", "sqlite3", "sqlcmd", "clickhouse-client", "clickhouse", "cockroach", "mongosh",
              "mongo", "redis-cli", "cqlsh", "duckdb", "pgcli", "mycli", "litecli", "usql", "bq", "snowsql", "trino", "osqueryi"}
MIGRATION_RESET_RE = re.compile(
    r"\bprisma\s+migrate\s+reset|\bprisma\s+db\s+push\b[^|;&]*--(?:force-reset|accept-data-loss)|\bprisma\s+db\s+execute"
    r"|\b(?:rails|rake|bin/rails)\s+db:(?:drop|reset|purge|schema:load|migrate:reset|seed:replant|truncate_all|rollback)"
    r"|manage\.py\s+(?:flush|reset_db|sqlflush|migrate\s+\S+\s+zero)|\balembic\s+downgrade|\bsequelize(?:-cli)?\s+db:(?:drop|migrate:undo)"
    r"|\bknex\s+migrate:(?:rollback|down)|\btypeorm\s+(?:schema:drop|migration:revert)|\bdrizzle-kit\s+(?:drop|push\b[^|;&]*--force)"
    r"|\bmix\s+ecto\.(?:drop|reset|rollback)|\bflask\s+db\s+downgrade|\bartisan\s+(?:migrate:(?:fresh|reset|rollback|refresh)|db:wipe)"
    r"|\bgoose\b[^|;&]*\s(?:down|reset|down-to)\b|\bmigrate\b[^|;&]*-database[^|;&]*\s(?:down|drop|force)\b|\bdbmate\s+(?:drop|rollback|down)"
    r"|\bliquibase\s+(?:dropAll|rollback\w*)|\bflyway\b[^|;&]*\bclean\b|\bdiesel\s+(?:database\s+reset|migration\s+(?:revert|redo))"
    r"|\bsqlx\s+database\s+(?:drop|reset)|doctrine:(?:database:drop|schema:drop)|\bdropdb\b|\bmysqladmin\b[^|;&]*\bdrop\b"
    r"|\bpg_restore\b[^|;&]*--clean|\bmongorestore\b[^|;&]*--drop|\bredis-cli\b[^|;&]*\b(?:flushall|flushdb)\b",
    re.I,
)


@cmd_rule
def production_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, c.args
    pos = _non_options(args)
    a = " ".join(pos[:4])
    out = []
    if prog in ("kubectl", "oc", "k"):
        pos = _skip_valued(args, {"--context", "-n", "--namespace", "--kubeconfig", "--cluster", "--user", "-s", "--server", "--token", "-l", "--selector", "-c", "--container"})
    if prog in ("kubectl", "oc", "k") and pos[:1] and pos[0] in KUBE_MUTATING:
        ctxname = next((args[i + 1] for i, x in enumerate(args[:-1]) if x == "--context"), "")
        out.append(Hit("production.kubernetes", "production", "ask", f"{prog} {pos[0]} changes a Kubernetes cluster" + (f" ({ctxname})" if ctxname else "")))
    if prog == "aws" and len(pos) >= 2 and (AWS_MUTATING_RE.match(pos[1]) or (pos[0] == "s3" and pos[1] == "cp" and pos[-1].startswith("s3://"))):
        out.append(Hit("production.cloud", "production", "ask", f"aws {pos[0]} {pos[1]} changes cloud resources"))
    if prog in ("gcloud", "az", "oci", "linode-cli", "ibmcloud", "aliyun", "scw", "vultr-cli", "exo") and set(pos) & CLOUD_MUTATING:
        out.append(Hit("production.cloud", "production", "ask", f"{prog} {a} changes cloud resources"))
    pattern = PAAS.get(prog)
    if pattern and re.search(pattern, " ".join(pos) if prog not in ("vercel",) else " ".join(args)):
        cat = "production"
        out.append(Hit("production.deploy" if prog not in ("terraform", "tofu", "terragrunt", "pulumi", "helm") else "production.infra", cat, "ask", f"{prog} {a or ''} deploys or changes live infrastructure".replace("  ", " ")))
    if prog in ("docker", "podman", "nerdctl"):
        if pos[:1] == ["push"]:
            out.append(Hit("release.image_push", "release", "ask", f"{prog} push publishes an image"))
        elif (pos[:2] in (["system", "prune"], ["volume", "prune"], ["volume", "rm"], ["image", "prune"], ["builder", "prune"], ["container", "prune"]) or (pos[:1] == ["compose"] and "down" in pos and _flags(args) & {"-v", "--volumes"})):
            out.append(Hit("destructive.docker_data", "destructive", "ask", f"{prog} {' '.join(pos[:2])} deletes containers' data or volumes"))
    if prog in DB_CLIENTS:
        if DESTRUCTIVE_SQL_RE.search(ctx.raw):
            out.append(Hit("production.destructive_sql", "production", "ask", f"{prog} runs SQL that drops, truncates or deletes data"))
        elif prog in ("psql", "mysql", "mongosh", "mongo", "redis-cli", "cockroach") and re.search(r"(?:prod|production|live)\b", " ".join(args), re.I):
            out.append(Hit("production.database", "production", "ask", f"{prog} connects to what looks like a production database"))
    return out


# ----------------------------------------------------------------------------- dependencies & release
INSTALL_SUBCOMMANDS = {
    "npm": {"install", "i", "add", "in", "isntall", "ins", "inst", "insta", "instal", "isnt", "isnta", "isntal"},
    "pnpm": {"add", "install", "i"}, "yarn": {"add"}, "bun": {"add", "install", "i"}, "deno": {"add", "install"},
    "pip": {"install"}, "pip3": {"install"}, "uv": {"add"}, "poetry": {"add"}, "pipenv": {"install"}, "pdm": {"add"},
    "conda": {"install"}, "mamba": {"install"}, "micromamba": {"install"}, "cargo": {"add", "install"}, "go": {"get", "install"},
    "gem": {"install"}, "bundle": {"add"}, "composer": {"require"}, "dotnet": {"add"}, "nuget": {"install"},
    "pipx": {"install", "run", "inject"}, "brew": {"install", "tap", "reinstall"}, "apt": {"install"}, "apt-get": {"install"},
    "yum": {"install"}, "dnf": {"install"}, "apk": {"add"}, "pacman": {"-S", "-Sy", "-Syu", "-U"}, "zypper": {"install", "in"},
    "port": {"install"}, "choco": {"install"}, "winget": {"install"}, "scoop": {"install"}, "snap": {"install"}, "flatpak": {"install"},
    "nix-env": {"-i", "-iA"}, "luarocks": {"install"}, "cpan": set(), "cpanm": set(), "Install-Module": set(), "Install-Package": set(),
}
SYSTEM_PKG = {"brew", "apt", "apt-get", "yum", "dnf", "apk", "pacman", "zypper", "port", "choco", "winget", "scoop", "snap", "flatpak", "nix-env"}
SELF_UPGRADE = {"pip", "setuptools", "wheel", "uv", "npm", "pnpm", "yarn", "corepack"}
REGISTRY_FLAGS = {"--index-url", "-i", "--extra-index-url", "--trusted-host", "--registry", "--find-links", "-f", "--source"}


def _install_targets(prog: str, args: list[str]) -> list[str]:
    """Package names a command adds, ignoring lockfile/requirements installs and local paths."""
    out, skip = [], False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a in ("-r", "--requirement", "-c", "--constraint", "-e", "--editable", "--prefix", "--target", "-t", "--python",
                 "--index-url", "-i", "--extra-index-url", "--trusted-host", "--registry", "--find-links", "-f", "--source",
                 "--save-prefix", "--tag", "--cwd", "-C", "--dir", "--filter", "-F", "--workspace", "-w", "--group", "-G", "--features"):
            skip = True
            continue
        if a.startswith("-"):
            continue
        out.append(a)
    names = []
    for t in out:
        if t in (".", "..") or t.startswith(("./", "../", "/", "file:")) or t.endswith((".txt", ".toml", ".cfg", ".lock")):
            continue
        names.append(t)
    return names


@cmd_rule
def dependency_rules(c: SimpleCmd, ctx: Ctx):
    prog, args = c.prog, list(c.args)
    out = []
    # `python -m pip install x`, `uv pip install x`
    if prog in INTERPRETERS and args[:2] == ["-m", "pip"]:
        prog, args = "pip", args[2:]
    if prog == "uv" and args[:1] == ["pip"]:
        prog, args = "pip", args[1:]
    if prog in ("npx", "pnpx", "bunx", "uvx") or (prog in ("pnpm", "yarn") and args[:1] == ["dlx"]) or (prog == "npm" and args[:1] in (["exec"], ["x"])):
        rest = args[1:] if prog in ("pnpm", "yarn", "npm") else args
        pkg = next((a for a in rest if not a.startswith("-")), "")
        name = re.sub(r"@[^@/]*$", "", pkg) if pkg.count("@") > (1 if pkg.startswith("@") else 0) else pkg
        if pkg and not (ctx.root / "node_modules" / ".bin" / name.split("/")[-1]).exists():
            out.append(Hit("dependencies.run_remote_package", "dependencies", "ask", f"{prog} downloads and runs the package '{pkg}', which isn't installed in this project"))
        return out
    subs = INSTALL_SUBCOMMANDS.get(prog)
    if subs is None:
        return None
    sub = next((a for a in args if not a.startswith("-") or a in subs), "")
    if subs and sub not in subs:
        if prog in ("npm", "pnpm", "yarn") and args[:2] in (["config", "set"],) and any("registry" in a for a in args):
            out.append(Hit("dependencies.registry", "dependencies", "ask", f"{prog} config changes the package registry"))
        if prog in ("npm", "yarn", "pnpm", "cargo", "gem", "poetry", "uv", "twine", "flit", "dotnet", "vsce", "ovsx") and sub in ("publish", "upload", "push"):
            out.append(Hit("release.package_publish", "release", "ask", f"{prog} {sub} publishes a package"))
        return out
    rest = args[args.index(sub) + 1:] if sub in args else args
    targets = _install_targets(prog, rest)
    if prog == "pip" and targets and all(re.split(r"[<>=!~\[]", t)[0].lower() in SELF_UPGRADE for t in targets):
        targets = []
    if prog == "go" and sub == "install":
        targets = [t for t in targets if "@" in t]
    flags = _flags(rest)
    if any(a.split("=", 1)[0] in REGISTRY_FLAGS for a in rest) or any(re.match(r"^(?:git\+|https?://|github:|gitlab:|bitbucket:|git://|ssh://)", t) for t in targets):
        out.append(Hit("dependencies.registry", "dependencies", "ask", "installs from a custom index, URL or git repository instead of the default registry"))
    if targets:
        cat, rid = ("machine", "machine.system_package") if prog in SYSTEM_PKG else ("dependencies", "dependencies.add")
        out.append(Hit(rid, cat, "ask", f"{prog} {sub} adds new package(s): {', '.join(targets[:5])}. Check the names for typos and lookalikes"))
    if flags & {"-g", "--global", "--location=global"} or "--break-system-packages" in flags or (prog == "pip" and "--user" in flags):
        out.append(Hit("machine.global_install", "machine", "ask", f"{prog} installs packages system-wide, outside the project"))
    return out


@cmd_rule
def release_rules(c: SimpleCmd, ctx: Ctx):
    prog, pos = c.prog, _non_options(c.args)
    if (prog in ("twine",) and pos[:1] == ["upload"]) or (prog in ("mvn",) and "deploy" in pos) or (prog in ("gradle", "./gradlew", "gradlew") and any(p.startswith("publish") for p in pos)):
        return Hit("release.package_publish", "release", "ask", f"{prog} publishes a package")
    return None


# ----------------------------------------------------------------------------- machine
SYSTEM_CHANGE = {
    "crontab": lambda p, a: "-l" not in a, "systemctl": lambda p, a: bool(p) and p[0] in ("enable", "disable", "start", "stop", "restart", "mask", "unmask", "daemon-reload", "edit", "kill", "isolate", "set-default", "link", "reenable"),
    "service": lambda p, a: len(p) > 1 and p[1] in ("start", "stop", "restart", "reload"), "launchctl": lambda p, a: bool(p) and p[0] in ("load", "unload", "bootstrap", "bootout", "enable", "disable", "remove", "submit", "kickstart"),
    "at": lambda p, a: True, "batch": lambda p, a: True, "chsh": lambda p, a: True, "usermod": lambda p, a: True, "useradd": lambda p, a: True, "userdel": lambda p, a: True,
    "groupadd": lambda p, a: True, "passwd": lambda p, a: True, "visudo": lambda p, a: True, "sysctl": lambda p, a: "-w" in a or any("=" in x for x in p),
    "iptables": lambda p, a: True, "ip6tables": lambda p, a: True, "nft": lambda p, a: True, "ufw": lambda p, a: True, "firewall-cmd": lambda p, a: True, "pfctl": lambda p, a: True,
    "mount": lambda p, a: bool(p), "umount": lambda p, a: True, "modprobe": lambda p, a: True, "insmod": lambda p, a: True, "rmmod": lambda p, a: True,
    "defaults": lambda p, a: bool(p) and p[0] in ("write", "delete"), "spctl": lambda p, a: True, "csrutil": lambda p, a: True, "nvram": lambda p, a: True,
    "xattr": lambda p, a: "-d" in a and any("quarantine" in x for x in p) or "-c" in a, "hostnamectl": lambda p, a: bool(p) and p[0].startswith("set"),
    "timedatectl": lambda p, a: bool(p) and p[0].startswith("set"), "update-alternatives": lambda p, a: True, "reg": lambda p, a: bool(p) and p[0].lower() in ("add", "delete", "import"),
    "Set-ExecutionPolicy": lambda p, a: True, "netsh": lambda p, a: True, "schtasks": lambda p, a: True, "sc": lambda p, a: bool(p) and p[0].lower() in ("create", "config", "delete", "stop"),
    "scutil": lambda p, a: "--set" in a, "pmset": lambda p, a: True, "dscl": lambda p, a: True, "chattr": lambda p, a: True,
}
PERSISTENCE_FILES = re.compile(r"(?:^|/)(?:\.bashrc|\.bash_profile|\.bash_login|\.profile|\.zshrc|\.zprofile|\.zshenv|\.zlogin|config\.fish|\.ssh/(?:authorized_keys2?|config|rc)|\.gitconfig|\.npmrc|\.pypirc|\.pip/pip\.conf|\.config/pip/pip\.conf|Library/LaunchAgents/[^/]+|\.config/autostart/[^/]+|\.config/systemd/user/[^/]+)$")


@cmd_rule
def machine_rules(c: SimpleCmd, ctx: Ctx):
    out = []
    if set(c.prefixes) & {"sudo", "doas", "pkexec"} or c.prog in ("su", "runas", "gsudo"):
        out.append(Hit("machine.sudo", "machine", "ask", "runs with administrator rights"))
    check = SYSTEM_CHANGE.get(c.prog)
    if check:
        pos = _non_options(c.args)
        try:
            if check(pos, c.args):
                out.append(Hit("machine.system_change", "machine", "ask", f"{c.prog} {' '.join(pos[:2])} changes system settings, services or scheduled jobs".replace("  ", " ")))
        except Exception:  # noqa: BLE001
            pass
    for e in c.prefixes:
        if e.startswith("env:") and re.match(r"env:(?:HUSKY=0|SKIP=|PRE_COMMIT_ALLOW_NO_CONFIG|LEFTHOOK=0|GIT_HOOKS_DISABLED|SKIP_HOOKS)", e):
            out.append(Hit("integrity.no_verify", "integrity", "ask", f"{e[4:]} switches off the repository's git hooks"))
    return out


# ----------------------------------------------------------------------------- whole-command rules
REMOTE_SCRIPT_RE = re.compile(
    r"\b(?:curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod|fetch)\b[^|;&]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh|dash|python3?|node|perl|ruby|iex|Invoke-Expression|pwsh|powershell)\b"
    r"|\b(?:sh|bash|zsh)\s+<\(\s*(?:curl|wget)\b|\b(?:sh|bash|zsh)\s+-c\s+[\"']?\$\(\s*(?:curl|wget)\b|\biex\s*\(|Invoke-Expression\s*\(?\s*\(?\s*(?:iwr|Invoke-WebRequest|New-Object\s+Net\.WebClient)",
    re.I,
)
FORK_BOMB_RE = re.compile(r"(\w+|:)\s*\(\)\s*\{\s*\1\s*\|\s*\1\s*&\s*\}\s*;\s*\1")
DEV_TCP_RE = re.compile(r"/dev/(?:tcp|udp)/")
PROC_ENVIRON_RE = re.compile(r"/proc/(?:self|\d+|\$\$|\*)/environ")


def _whole_command_hits(raw: str) -> list[Hit]:
    out = []
    if REMOTE_SCRIPT_RE.search(raw):
        out.append(Hit("dependencies.remote_script", "dependencies", "ask", "runs a script downloaded from the internet"))
    if FORK_BOMB_RE.search(raw):
        out.append(Hit("destructive.fork_bomb", "destructive", "deny", "fork bomb: exhausts the machine's processes"))
    if DEV_TCP_RE.search(raw):
        out.append(Hit("exfiltration.raw_socket", "exfiltration", "ask", "opens a network connection through /dev/tcp"))
    if PROC_ENVIRON_RE.search(raw):
        out.append(Hit("secrets.token_command", "secrets", "deny", "reads a process's environment variables, which hold secrets"))
    if MIGRATION_RESET_RE.search(raw):
        out.append(Hit("production.migration_reset", "production", "ask", "resets, rolls back or drops a database"))
    return out


def evaluate(analysis: ShellAnalysis, command: str, root: str | Path) -> list[Hit]:
    """All rule hits for a command line (before policy overrides)."""
    ctx = Ctx(root=Path(root).resolve(), raw=command)
    hits: list[Hit] = []
    for c in analysis.commands:
        if not c.words:
            continue
        for rule in _CMD_RULES:
            try:
                r = rule(c, ctx)
            except Exception:  # noqa: BLE001 - one broken rule must not disable the others
                continue
            if r is None:
                continue
            hits.extend([r] if isinstance(r, Hit) else r)
    hits.extend(_whole_command_hits(command))
    for h in analysis.hidden:
        hits.append(Hit("hidden." + re.sub(r"\W+", "_", h.split(",")[0].lower()).strip("_")[:40], "hidden", "ask", f"Chronos can't check this command statically: it {h}"))
    for w in analysis.writes:
        if PERSISTENCE_FILES.search(w):
            deny = "authorized_keys" in w
            hits.append(Hit("machine.persistence", "machine", "deny" if deny else "ask", f"writes {w}, which runs or grants access every time you log in or use git"))
    seen, uniq = set(), []
    for h in hits:
        if (h.rule, h.reason) not in seen:
            seen.add((h.rule, h.reason))
            uniq.append(h)
    return uniq


def apply_overrides(hits: list[Hit], overrides: dict[str, str] | None) -> list[Hit]:
    """Apply `policy.rules` (rule id > category > `*`). `off` drops the hit."""
    if not overrides:
        return hits
    out = []
    for h in hits:
        d = overrides.get(h.rule) or overrides.get(h.category) or overrides.get("*") or h.decision
        if d == "off":
            continue
        out.append(Hit(h.rule, h.category, d if d in ("deny", "ask", "log") else h.decision, h.reason))
    return out


MCP_RISKY_RE = re.compile(r"(?:^|_)(?:delete|drop|destroy|remove|purge|truncate|reset|wipe|terminate|kill|merge|force_push|push|deploy|publish|release|transfer|pay|refund|charge|send_email|send_message|execute_sql|run_sql|execute_query|apply_migration|update_secret|set_secret|revoke|ban|archive)(?:_|$)", re.I)


def evaluate_mcp(tool_name: str, tool_input: dict) -> list[Hit]:
    """MCP tools are named `mcp__<server>__<tool>`; ask for ones whose name says they change or destroy things."""
    parts = tool_name.split("__")
    if len(parts) < 3 or parts[1] == "chronos":
        return []
    tool = parts[-1]
    hits = []
    text = " ".join(str(v) for v in (tool_input or {}).values() if isinstance(v, (str, int, float)))
    if MCP_RISKY_RE.search(tool):
        hits.append(Hit("mcp.risky_tool", "mcp", "ask", f"MCP tool {parts[1]}.{tool} can change or delete data outside this repository"))
    elif re.search(r"(?:sql|query|execute)", tool, re.I) and DESTRUCTIVE_SQL_RE.search(text):
        hits.append(Hit("mcp.destructive_sql", "mcp", "ask", f"MCP tool {parts[1]}.{tool} runs SQL that drops, truncates or deletes data"))
    if EXFIL_HOSTS_RE.search(text):
        hits.append(Hit("exfiltration.paste_host", "exfiltration", "deny", "sends data to a paste, webhook or tunnel service"))
    names = find_secret_names(text)
    if names:
        hits.append(Hit("secrets.mcp_secret", "secrets", "ask", f"passes secret file name(s) to an MCP tool: {', '.join(names[:3])}"))
    return hits
