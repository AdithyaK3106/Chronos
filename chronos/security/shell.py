"""Static analysis of shell commands for the Bash firewall.

Finds what a command line *writes* (redirections, rm/mv/cp/tee/sed -i/git checkout -- ...) and which
*secret files it mentions*, so the Claude Code hook can apply the same rules to `Bash` as to the
Edit/Write/Read tools.

This is a best-effort parser, not a shell. It handles pipelines, `;`/`&&`/`||`, redirections, `cd`,
command substitution, `sh -c`/`eval` strings, heredocs, simple globs and `$VAR`/`~`. It cannot see
what a script or program does once it runs (`python fix.py`, `make`, recursive `grep -r`). Writes
it misses are caught afterwards by the PostToolUse backstop (`changed_sensitive_files`); reads it
misses are what the `chronos exec` sandbox is for.
"""

from __future__ import annotations

import glob as _glob
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from chronos.security.sanitizer import is_env_template

OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;"}
REDIRECTS_WRITE = {">", ">>", "&>", "&>>", ">|"}
REDIRECTS_READ = {"<", "<<<"}
FD_DUP = {">&", "<&"}

# Wrappers that run the rest of the line as a command.
PREFIX_COMMANDS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "ionice", "stdbuf", "timeout", "xargs", "doas"}
# Arguments of these never read file contents, so naming a secret there is harmless.
METADATA_COMMANDS = {"ls", "stat", "test", "[", "[[", "file", "du", "echo", "printf", "which", "type", "basename", "dirname", "realpath", "readlink"}
# Every non-option argument is written (or removed).
WRITE_ALL = {"touch", "truncate", "rm", "rmdir", "unlink", "shred", "chmod", "chown", "chgrp", "tee", "vi", "vim", "nvim", "nano", "emacs", "code"}
# The last non-option argument is the destination.
WRITE_LAST = {"cp", "ln", "install", "rsync", "scp"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh"}
GIT_WRITE_PATHS = {"checkout", "restore", "rm", "mv"}
GIT_OPAQUE_WRITES = {"reset", "clean", "stash", "apply", "am", "merge", "rebase", "pull", "cherry-pick", "revert", "switch"}

# Secret file names (cf. sanitizer.DEFAULT_DENIED_PATTERNS), searched for inside any token so that
# `python -c "open('.env')"` and `F=.env` are caught too.
_B = r"(?:^|(?<=[\s/'\"=(:,<>@]))"
_E = r"(?=$|[\s'\"),;:<>|&])"
SECRET_NAME_RE = re.compile(
    _B + r"("
    r"\.env(?:\.[\w.-]+)?|[\w.-]+\.env(?:\.[\w.-]+)?"
    r"|[\w.-]+\.pem|[\w.-]+\.key"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)[\w.-]*"
    r"|credentials(?:\.[\w.-]+)?"
    r"|secrets/[^\s'\"]*"
    r")" + _E
)
_SUBST_RE = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1")


@dataclass
class ShellAnalysis:
    writes: list[str] = field(default_factory=list)   # absolute paths the command writes or removes
    secrets: list[str] = field(default_factory=list)  # secret file names it mentions
    opaque: list[str] = field(default_factory=list)   # constructs whose effects can't be known statically

    def merge(self, other: "ShellAnalysis") -> None:
        for name in ("writes", "secrets", "opaque"):
            mine = getattr(self, name)
            mine.extend(x for x in getattr(other, name) if x not in mine)


def find_secret_names(text: str) -> list[str]:
    return [m for m in SECRET_NAME_RE.findall(text) if not is_env_template(m)]


INTERPRETERS = {"python", "python3", "node", "ruby", "perl", "php", "deno", "bun"}


def _strip_heredocs(cmd: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies from the command line. Returns (command, [(program, body)])."""
    lines = cmd.split("\n")
    out: list[str] = []
    bodies: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delims = [m.group(2) for m in _HEREDOC_RE.finditer(line)]
        first = line.strip().split()[0] if line.strip() else ""
        prog = Path(first).name
        i += 1
        for d in delims:
            body = []
            while i < len(lines) and lines[i].strip() != d:
                body.append(lines[i])
                i += 1
            i += 1  # the delimiter line
            bodies.append((prog, "\n".join(body)))
    return "\n".join(out), bodies


def _tokens(cmd: str) -> list[str]:
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    lex.commenters = "#"
    try:
        return list(lex)
    except ValueError:  # unbalanced quotes: fall back to whitespace splitting
        return cmd.split()


def _split_commands(tokens: list[str]) -> list[list[str]]:
    cmds: list[list[str]] = [[]]
    for t in tokens:
        if t in OPERATORS:
            cmds.append([])
        else:
            cmds[-1].append(t)
    return [c for c in cmds if c]


def _expand(token: str, cwd: Path) -> list[Path]:
    """Resolve a word to paths: ~, $VARS from the environment, globs. Unknown $VARS -> []."""
    word = os.path.expanduser(os.path.expandvars(token))
    if "$" in word:
        return []
    p = Path(word)
    base = p if p.is_absolute() else cwd / p
    if any(ch in word for ch in "*?["):
        matches = sorted(_glob.glob(str(base)))[:200]
        if matches:
            return [Path(m) for m in matches]
    return [base]


def _non_options(args: list[str]) -> list[str]:
    out, after_dd = [], False
    for a in args:
        if a == "--" and not after_dd:
            after_dd = True
            continue
        if not after_dd and a.startswith("-") and a != "-":
            continue
        out.append(a)
    return out


def _in_place(prog: str, args: list[str]) -> bool:
    if prog == "sed":
        return any(a.startswith("-i") or a.startswith("--in-place") or (re.match(r"^-[a-zA-Z]*i", a) and not a.startswith("--")) for a in args)
    return any(re.match(r"^-[a-zA-Z]*i", a) for a in args)  # perl -i, -pi, -pie


def _edited_files(args: list[str]) -> list[str]:
    """Files of `sed -i` / `perl -pi`: the non-option arguments minus the script."""
    scripts: set[int] = set()
    explicit = False
    for idx, a in enumerate(args):
        if a in ("-e", "-f", "--expression", "--file") or (re.match(r"^-[a-zA-Z]*e$", a) and not a.startswith("--")):
            explicit = True
            scripts.add(idx + 1)
    files = [a for idx, a in enumerate(args) if idx not in scripts and a and not (a.startswith("-") and a != "-")]
    return files if explicit else files[1:]


def is_temp_path(p: str | Path) -> bool:
    """System temp dirs and /dev: writes there (test output, /dev/null) are routine."""
    s = str(p)
    temps = {tempfile.gettempdir(), "/tmp", "/var/tmp", "/dev"}
    return any(s == t or s.startswith(t.rstrip("/") + "/") for t in temps)


def analyze(command: str, cwd: str | Path, _depth: int = 0) -> ShellAnalysis:
    """Analyse a shell command line run in `cwd`."""
    res = ShellAnalysis()
    if not command or _depth > 3:
        return res
    cwd = Path(cwd)
    cmd, bodies = _strip_heredocs(command)
    for prog, body in bodies:
        # A heredoc fed to `cat > file` is data; fed to a shell or interpreter it is code.
        if prog in SHELLS:
            res.merge(analyze(body, cwd, _depth + 1))
        elif prog in INTERPRETERS:
            res.secrets.extend(s for s in find_secret_names(body) if s not in res.secrets)
            res.opaque.append(prog)

    # Command substitutions run as commands of their own.
    def _sub(m: re.Match) -> str:
        res.merge(analyze(m.group(1) or m.group(2) or "", cwd, _depth + 1))
        return " __CHRONOS_SUBST__ "

    cmd = _SUBST_RE.sub(_sub, cmd)

    for argv in _split_commands(_tokens(cmd)):
        cwd = _analyze_simple(argv, cwd, res, _depth)
    return res


def _analyze_simple(argv: list[str], cwd: Path, res: ShellAnalysis, depth: int) -> Path:
    """One simple command. Returns the working directory for the next command (after `cd`)."""
    words: list[str] = []
    i = 0
    while i < len(argv):
        t = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if t in REDIRECTS_WRITE and nxt is not None:
            res.writes.extend(str(p) for p in _expand(nxt, cwd))
            res.secrets.extend(s for s in find_secret_names(nxt) if s not in res.secrets)
            i += 2
            continue
        if t in REDIRECTS_READ or t in FD_DUP or t == "<<":
            if nxt is not None and t in REDIRECTS_READ:
                res.secrets.extend(s for s in find_secret_names(nxt) if s not in res.secrets)
            i += 2
            continue
        words.append(t)
        i += 1

    # Leading VAR=value assignments.
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        res.secrets.extend(s for s in find_secret_names(words[0]) if s not in res.secrets)
        words = words[1:]
    # Wrappers: sudo, env, timeout 10, xargs -0 ...
    while words and Path(words[0]).name in PREFIX_COMMANDS:
        name = Path(words[0]).name
        words = words[1:]
        if name == "xargs":
            res.opaque.append("xargs")
        while words and name in ("sudo", "doas") and words[0] in ("-u", "-g", "-C", "-h", "-p"):
            words = words[2:]
        while words and (words[0].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[0]) or (name == "timeout" and re.match(r"^\d", words[0]))):
            words = words[1:]
    if not words:
        return cwd

    prog = Path(words[0]).name
    args = words[1:]

    if prog == "cd":
        target = _non_options(args)
        if target and target[0] != "-":
            dests = _expand(target[0], cwd)
            if dests:
                return dests[0]
        return cwd

    if prog not in METADATA_COMMANDS:
        for a in args:
            res.secrets.extend(s for s in find_secret_names(a) if s not in res.secrets)

    if prog in SHELLS or prog == "eval":
        if prog == "eval":
            res.merge(analyze(" ".join(args), cwd, depth + 1))
        elif "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args):
                res.merge(analyze(args[idx + 1], cwd, depth + 1))
        elif _non_options(args):
            res.opaque.append(f"{prog} script")
        return cwd

    files = _non_options(args)
    targets: list[str] = []
    if prog in WRITE_ALL:
        targets = files
    elif prog in WRITE_LAST and len(files) >= 2:
        targets = files[-1:]
    elif prog == "mv":
        targets = files
    elif prog in ("sed", "perl") and _in_place(prog, args):
        targets = _edited_files(args)
    elif prog == "dd":
        targets = [a[3:] for a in args if a.startswith("of=")]
    elif prog == "git" and args:
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in GIT_WRITE_PATHS:
            rest = args[args.index(sub) + 1:]
            if "--" in rest:
                targets = [a for a in rest[rest.index("--") + 1:]]
            elif sub in ("rm", "mv", "restore"):
                targets = _non_options(rest)
            else:  # `git checkout <branch>` vs `git checkout <file>`: only existing paths
                targets = [a for a in _non_options(rest) if (cwd / a).exists()]
        elif sub in GIT_OPAQUE_WRITES:
            res.opaque.append(f"git {sub}")
    elif prog in ("patch",):
        res.opaque.append("patch")
    elif prog in ("python", "python3", "node", "ruby", "perl", "make", "npm", "npx", "pnpm", "yarn", "uv", "pip", "cargo", "go"):
        res.opaque.append(prog)

    for t in targets:
        res.writes.extend(str(p) for p in _expand(t, cwd))
    return cwd
