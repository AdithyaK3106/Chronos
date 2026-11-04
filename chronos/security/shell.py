"""Static analysis of shell commands for the Bash firewall.

Finds what a command line *writes* (redirections, rm/mv/cp/tee/sed -i/git checkout -- ...), which
*secret files it mentions*, every *simple command* it runs (for the threat rules in
`chronos.security.command_rules`), and constructs that *hide* what will run (`base64 -d | sh`,
`python -c "open('.'+'env')"`, `$'\\x2e\\x65nv'`), so the Claude Code hook can apply the same rules to
`Bash` as to the Edit/Write/Read tools.

This is a best-effort parser, not a shell. It handles pipelines, `;`/`&&`/`||`, redirections, `cd`,
command substitution, `sh -c`/`eval` strings, heredocs, `find -exec`, `xargs`, simple globs and
`$VAR`/`~`. It cannot see what a script or program does once it runs (`python fix.py`, `make`).
Writes it misses are caught afterwards by the PostToolUse backstop; commands whose effect is
deliberately hidden are reported in `hidden`, and the hook asks the developer about them.
"""

from __future__ import annotations

import fnmatch
import glob as _glob
import os
import re
import shlex
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from chronos.security.sanitizer import is_env_template

OPERATORS = {";", "&&", "||", "|", "&", "|&", "(", ")", "\n", ";;"}
PIPES = {"|", "|&"}
REDIRECTS_WRITE = {">", ">>", "&>", "&>>", ">|"}
REDIRECTS_READ = {"<", "<<<"}
FD_DUP = {">&", "<&"}

# Wrappers that run the rest of the line as a command.
PREFIX_COMMANDS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "ionice", "stdbuf", "timeout", "xargs", "doas", "pkexec", "chroot", "nsenter", "unbuffer", "caffeinate", "watch"}
# Arguments of these never read file contents, so naming a secret there is harmless.
METADATA_COMMANDS = {"ls", "stat", "test", "[", "[[", "file", "du", "echo", "printf", "which", "type", "basename", "dirname", "realpath", "readlink"}
# Every non-option argument is written (or removed).
WRITE_ALL = {"touch", "truncate", "rm", "rmdir", "unlink", "shred", "chmod", "chown", "chgrp", "tee", "vi", "vim", "nvim", "nano", "emacs", "code"}
# The last non-option argument is the destination.
WRITE_LAST = {"cp", "ln", "install", "rsync", "scp"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "pwsh", "powershell"}
INTERPRETERS = {"python", "python3", "python2", "node", "ruby", "perl", "php", "deno", "bun", "lua", "Rscript", "osascript"}
GIT_WRITE_PATHS = {"checkout", "restore", "rm", "mv"}
GIT_OPAQUE_WRITES = {"reset", "clean", "stash", "apply", "am", "merge", "rebase", "pull", "cherry-pick", "revert", "switch"}
# Options whose value is free text (a commit message, a PR title), never a file name.
MESSAGE_OPTIONS = {"-m", "--message", "--title", "--body", "-t", "-b", "--notes", "--subject", "--description"}
MESSAGE_PROGRAMS = {"git", "gh", "glab", "hub", "jj", "hg", "svn"}

# Secret file names (cf. sanitizer.DEFAULT_DENIED_PATTERNS), searched for inside any token so that
# `python -c "open('.env')"` and `F=.env` are caught too.
_B = r"(?:^|(?<=[\s/'\"=(:,<>@]))"
_E = r"(?=$|[\s'\"),;:<>|&])"
SECRET_NAME_RE = re.compile(
    _B + r"("
    r"\.env(?:\.[\w.-]+)?|[\w.-]+\.env(?:\.[\w.-]+)?"
    r"|[\w.-]+\.pem|[\w.-]+\.key"
    r"|id_(?:rsa|dsa|ecdsa|ed25519)(?![\w.-]*\.pub" + _E + r")[\w.-]*"
    r"|credentials(?:\.[\w.-]+)?|[\w.-]+[_-]credentials(?:\.[\w.-]+)?"
    r"|secrets/[^\s'\"]*|secrets\.(?:ya?ml|json|toml)"
    r"|[\w.-]+\.tfstate(?:\.backup)?"
    r"|\.netrc|_netrc|\.git-credentials|\.htpasswd|\.pgpass|\.my\.cnf"
    r"|[\w.-]+\.(?:p12|pfx|jks|keystore|ppk|kdbx|asc\.key)"
    r"|service-account[\w.-]*\.json|oauth_creds\.json|\.credentials\.json"
    r"|kubeconfig(?:\.[\w.-]+)?"
    r")" + _E
)
# Credential stores addressed by directory (home-relative): any file in them is secret, except
# public material (`.pub`, `known_hosts`, ssh `config`).
CRED_DIR_RE = re.compile(
    r"(?:^|[\s'\"=:])(?:~|\$HOME|\$\{HOME\}|/(?:home|Users)/[^/\s]+|/root)?/?"
    r"(\.ssh/(?!known_hosts|config\b|[\w.-]+\.pub\b|authorized_keys)[\w.-]+|\.aws/(?:credentials|sso/cache)[\w./-]*"
    r"|\.kube/config|\.docker/config\.json|\.config/gh/hosts\.yml|\.config/gcloud/[\w./-]*"
    r"|\.gnupg/[\w./-]*|\.password-store/[\w./-]*|\.codex/auth\.json|\.claude/\.credentials\.json"
    r"|\.gemini/oauth_creds\.json|Library/Keychains/[\w./-]*|\.azure/[\w./-]*|\.config/op/[\w./-]*)"
)
_SUBST_RE = re.compile(r"\$\(([^()]*(?:\([^()]*\)[^()]*)*)\)|`([^`]*)`")
_PROC_SUBST_RE = re.compile(r"(?:^|[\s;&|])(?:source|\.|sh|bash|zsh|dash|ksh|python3?|node|ruby|perl)\s+<\(")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w]*)\1")
_ANSI_C_RE = re.compile(r"\$'[^']*\\(?:x[0-9A-Fa-f]{1,2}|[0-7]{1,3}|u[0-9A-Fa-f]{4})")
# Tools that turn data into text the shell will then use as code or as a file name.
_DECODER_RE = re.compile(
    r"\bbase(?:64|32)\b[^|;&]*(?:\s-d\b|\s--decode\b|\s-D\b)|\bxxd\b[^|;&]*\s-r|\bopenssl\b[^|;&]*\s-d\b|\brev\b"
    r"|\bprintf\b[^|;&]*\\(?:x|[0-7])|\btr\b|\bgunzip\b|\bzcat\b|\bperl\b[^|;&]*\bpack\b|\bpython3?\b[^|;&]*b64decode"
)
# Primitives inside `python -c`, `node -e`, ... that touch files, processes, the network or secrets.
_INLINE_DANGER_RE = re.compile(
    r"\bopen\s*\(|\.write|\.unlink|\bremove\b|\brmtree\b|\brename\b|\bchmod\b|\bsubprocess\b|\bos\.system\b|\bpopen\b"
    r"|\bexec\s*\(|\beval\s*\(|__import__|\bsocket\b|\burllib\b|\brequests\b|\bhttpx?\b|\bfetch\s*\(|child_process"
    r"|\bfs\.|\brequire\s*\(\s*['\"](?:fs|child_process|net|http|https)|b64decode|\bgetenv\b|\benviron\b|process\.env"
    r"|\bshutil\b|\bFile\.(?:read|write|delete)|\bIO\.(?:read|write)|`|\bsystem\s*\(|\bunlink\b|read_text|write_text"
)
INLINE_CODE_FLAGS = {"python": {"-c"}, "python3": {"-c"}, "python2": {"-c"}, "node": {"-e", "--eval", "-p", "--print"},
                     "ruby": {"-e"}, "perl": {"-e", "-E"}, "php": {"-r"}, "deno": {"eval"}, "bun": {"-e", "--eval"},
                     "lua": {"-e"}, "osascript": {"-e"}}
# Programs that print or copy the content of the files they are given.
READERS = {"cat", "head", "tail", "less", "more", "bat", "batcat", "nl", "tac", "od", "xxd", "hexdump", "strings",
           "base64", "base32", "grep", "egrep", "fgrep", "rg", "ag", "ack", "awk", "gawk", "sed", "cut", "sort", "uniq",
           "diff", "cmp", "jq", "yq", "cp", "tar", "zip", "gzip", "7z", "curl", "wget", "scp", "rsync", "openssl",
           "source", ".", "vim", "vi", "nano", "view", "wc", "md5sum", "sha256sum", "gpg"}
# Secret file names to test glob patterns against (as `find -name` would match them).
SECRET_SAMPLES = [".env", ".env.local", ".env.production", "prod.env", "id_rsa", "id_ed25519", "server.pem",
                  "server.key", "credentials", "credentials.json", "terraform.tfstate", ".netrc", ".git-credentials",
                  "cert.p12", "service-account.json", "secrets.yaml"]


@dataclass
class SimpleCmd:
    """One simple command after wrappers are removed: `sudo -u x timeout 5 rm -rf y` -> words [rm, -rf, y]."""

    words: list[str]
    cwd: Path
    piped: bool = False                                  # its stdin comes from a pipe
    prefixes: list[str] = field(default_factory=list)    # wrappers it ran under (sudo, xargs, find-exec ...)

    @property
    def prog(self) -> str:
        return Path(self.words[0]).name if self.words else ""

    @property
    def args(self) -> list[str]:
        return self.words[1:]


@dataclass
class ShellAnalysis:
    writes: list[str] = field(default_factory=list)   # absolute paths the command writes or removes
    secrets: list[str] = field(default_factory=list)  # secret file names it mentions
    opaque: list[str] = field(default_factory=list)   # programs whose effects can't be known statically
    hidden: list[str] = field(default_factory=list)   # constructs that hide what will run (ask the developer)
    commands: list[SimpleCmd] = field(default_factory=list)

    def merge(self, other: "ShellAnalysis") -> None:
        for name in ("writes", "secrets", "opaque", "hidden"):
            mine = getattr(self, name)
            mine.extend(x for x in getattr(other, name) if x not in mine)
        self.commands.extend(other.commands)

    def _add(self, name: str, items) -> None:
        mine = getattr(self, name)
        mine.extend(x for x in items if x not in mine)


def find_secret_names(text: str) -> list[str]:
    names = [m for m in SECRET_NAME_RE.findall(text) if not is_env_template(m)]
    names += [m for m in CRED_DIR_RE.findall(text) if m not in names]
    return names


def _prose_secret_names(text: str) -> list[str]:
    """Secret names in free text (`-m "..."`): only path-like mentions count, and only if the
    text is a single word. A sentence that mentions `.env` is a message, not a file access."""
    if re.search(r"\s", text.strip()):
        return []
    return find_secret_names(text)


def glob_matches_secret(pattern: str, find_semantics: bool = False) -> list[str]:
    """Secret file names a glob pattern would match. With shell semantics a leading `*` does not
    match dotfiles; `find -name` matches them."""
    name = pattern.rsplit("/", 1)[-1]
    if not any(ch in name for ch in "*?["):
        return []
    hits = []
    for s in SECRET_SAMPLES:
        if not find_semantics and s.startswith(".") and not name.startswith("."):
            continue
        if fnmatch.fnmatch(s, name) and not is_env_template(s):
            hits.append(s)
    return hits


def _strip_heredocs(cmd: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies from the command line. Returns (command, [(program, body)])."""
    lines = cmd.split("\n")
    out: list[str] = []
    bodies: list[tuple[str, str]] = []
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


def _split_commands(tokens: list[str]) -> list[tuple[list[str], bool]]:
    """Simple commands, each with whether its input is piped from the previous one."""
    cmds: list[tuple[list[str], bool]] = [([], False)]
    for t in tokens:
        if t in OPERATORS:
            cmds.append(([], t in PIPES))
        else:
            cmds[-1][0].append(t)
    return [(c, p) for c, p in cmds if c]


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
        if command and _depth > 3:
            res.hidden.append("nesting deeper than Chronos follows")
        return res
    cwd = Path(cwd)
    cmd, bodies = _strip_heredocs(command)
    for prog, body in bodies:
        # A heredoc fed to `cat > file` is data; fed to a shell or interpreter it is code.
        if prog in SHELLS:
            res.merge(analyze(body, cwd, _depth + 1))
        elif prog in INTERPRETERS:
            res._add("secrets", find_secret_names(body))
            res.opaque.append(prog)
            if _INLINE_DANGER_RE.search(body):
                res._add("hidden", [f"{prog} code that touches files, processes or the network"])

    if _ANSI_C_RE.search(cmd):
        res._add("hidden", ["$'...' escape sequences that spell out text at runtime"])
    if _PROC_SUBST_RE.search(cmd):
        res._add("hidden", ["runs the output of another command as a script (<(...))"])

    # Command substitutions run as commands of their own.
    def _sub(m: re.Match) -> str:
        inner = m.group(1) or m.group(2) or ""
        res.merge(analyze(inner, cwd, _depth + 1))
        if _DECODER_RE.search(inner):
            res._add("hidden", ["builds a command or file name at runtime by decoding text"])
        return " __CHRONOS_SUBST__ "

    cmd = _SUBST_RE.sub(_sub, cmd)

    for argv, piped in _split_commands(_tokens(cmd)):
        cwd = _analyze_simple(argv, cwd, res, _depth, piped)
    return res


def _secret_scan_args(prog: str, args: list[str], res: ShellAnalysis) -> None:
    skip_next = False
    for idx, a in enumerate(args):
        if skip_next:
            skip_next = False
            res._add("secrets", _prose_secret_names(a))
            continue
        if prog in MESSAGE_PROGRAMS:
            if a in MESSAGE_OPTIONS:
                skip_next = True
                continue
            if re.match(r"^--(?:message|title|body|notes)=", a):
                res._add("secrets", _prose_secret_names(a.split("=", 1)[1]))
                continue
        res._add("secrets", find_secret_names(a))


def _analyze_simple(argv: list[str], cwd: Path, res: ShellAnalysis, depth: int, piped: bool = False, prefixes: list[str] | None = None) -> Path:
    """One simple command. Returns the working directory for the next command (after `cd`)."""
    prefixes = list(prefixes or [])
    words: list[str] = []
    i = 0
    while i < len(argv):
        t = argv[i]
        nxt = argv[i + 1] if i + 1 < len(argv) else None
        if t in REDIRECTS_WRITE and nxt is not None:
            res.writes.extend(str(p) for p in _expand(nxt, cwd))
            res._add("secrets", find_secret_names(nxt))
            i += 2
            continue
        if t in REDIRECTS_READ or t in FD_DUP or t == "<<":
            if nxt is not None and t in REDIRECTS_READ:
                res._add("secrets", find_secret_names(nxt))
                res._add("secrets", glob_matches_secret(nxt) and [n.name for n in _expand(nxt, cwd) if find_secret_names(n.name)])
            i += 2
            continue
        words.append(t)
        i += 1

    # Leading VAR=value assignments.
    env_assign: list[str] = []
    while words and re.match(r"^[A-Za-z_]\w*=", words[0]):
        res._add("secrets", find_secret_names(words[0]))
        env_assign.append(words[0])
        words = words[1:]
    # Wrappers: sudo, env, timeout 10, xargs -0 ...
    while words and Path(words[0]).name in PREFIX_COMMANDS:
        name = Path(words[0]).name
        prefixes.append(name)
        words = words[1:]
        if name == "xargs":
            res.opaque.append("xargs")
        while words and name in ("sudo", "doas") and words[0] in ("-u", "-g", "-C", "-h", "-p"):
            words = words[2:]
        while words and (words[0].startswith("-") or re.match(r"^[A-Za-z_]\w*=", words[0]) or (name in ("timeout", "nice", "watch") and re.match(r"^\d", words[0]))):
            if re.match(r"^[A-Za-z_]\w*=", words[0]):
                env_assign.append(words[0])
            # xargs -I {} / -n 1 / -P 4 take a value
            if name == "xargs" and words[0] in ("-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"):
                words = words[1:]
            words = words[1:]
    if not words:
        if prefixes and prefixes[-1] == "env":
            res.commands.append(SimpleCmd(words=["env"], cwd=cwd, piped=piped, prefixes=prefixes[:-1]))
        return cwd

    cmd = SimpleCmd(words=list(words), cwd=cwd, piped=piped, prefixes=prefixes)
    if env_assign:
        cmd.prefixes = prefixes + [f"env:{a}" for a in env_assign]
    res.commands.append(cmd)
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
        _secret_scan_args(prog, args, res)
        # Globs that expand to secret files (`cat .e*`): check what they match on disk.
        for a in args:
            if any(ch in a for ch in "*?[") and not a.startswith("-"):
                for p in _expand(a, cwd):
                    res._add("secrets", find_secret_names(p.name))

    # Data piped into a shell or interpreter runs as code: `curl ... | sh`, `base64 -d | bash`.
    if piped and (prog in SHELLS or prog in INTERPRETERS):
        flags = INLINE_CODE_FLAGS.get(prog, set()) | {"-c"}
        script = [a for a in _non_options(args) if a != "-"]
        if not any(a in flags for a in args) and not script:
            res._add("hidden", [f"pipes text into {prog}, which runs it as code"])
    if "xargs" in prefixes and (prog in SHELLS or prog in INTERPRETERS):
        res._add("hidden", [f"xargs feeds computed arguments to {prog}"])

    if prog in SHELLS or prog == "eval" or prog in ("source", "."):
        if prog == "eval":
            joined = " ".join(args)
            if "$" in joined or "__CHRONOS_SUBST__" in joined:
                res._add("hidden", ["eval of text computed at runtime"])
            res.merge(analyze(joined, cwd, depth + 1))
        elif "-c" in args:
            idx = args.index("-c")
            if idx + 1 < len(args):
                code = args[idx + 1]
                if "__CHRONOS_SUBST__" in code or re.search(r"\$\{?[A-Za-z_]", code):
                    res._add("hidden", [f"{prog} -c with text computed at runtime"])
                res.merge(analyze(code, cwd, depth + 1))
        elif prog in ("pwsh", "powershell") and any(a.lower() in ("-command", "-c", "-encodedcommand", "-enc", "-e") for a in args):
            res._add("hidden", [f"{prog} command line"])
        elif _non_options(args):
            res.opaque.append(f"{prog} script")
        return cwd

    if prog in INLINE_CODE_FLAGS:
        flags = INLINE_CODE_FLAGS[prog]
        for idx, a in enumerate(args):
            if a in flags and idx + 1 < len(args):
                code = args[idx + 1]
                if _INLINE_DANGER_RE.search(code):
                    res._add("hidden", [f"{prog} {a} code that touches files, processes or the network"])
                break

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
        res._add("secrets", [s for a in args if a.startswith("if=") for s in find_secret_names(a[3:])])
    elif prog == "find":
        _analyze_find(args, cwd, res, depth, prefixes)
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
    elif prog in INTERPRETERS or prog in ("make", "npm", "npx", "pnpm", "yarn", "uv", "pip", "pip3", "cargo", "go", "gradle", "mvn"):
        res.opaque.append(prog)

    for t in targets:
        res.writes.extend(str(p) for p in _expand(t, cwd))
    return cwd


def _analyze_find(args: list[str], cwd: Path, res: ShellAnalysis, depth: int, prefixes: list[str]) -> None:
    """`find`: name patterns that match secret files, `-delete`, and the `-exec` command."""
    patterns = [args[i + 1] for i, a in enumerate(args[:-1]) if a in ("-name", "-iname", "-path", "-ipath", "-wholename")]
    secret_hits = [s for p in patterns for s in glob_matches_secret(p, find_semantics=True)]
    secret_hits += [s for p in patterns for s in find_secret_names(p)]
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-exec", "-execdir", "-ok", "-okdir"):
            j = i + 1
            inner: list[str] = []
            while j < len(args) and args[j] not in (";", "\\;", "+"):
                inner.append(args[j])
                j += 1
            if inner:
                prog = Path(inner[0]).name
                if prog in SHELLS or prog in INTERPRETERS:
                    res._add("hidden", [f"find -exec runs {prog} on every matching file"])
                if prog in READERS:
                    if secret_hits:
                        res._add("secrets", secret_hits)
                    elif not patterns:
                        res._add("hidden", [f"find -exec {prog} reads every file under the directory, secret files included"])
                _analyze_simple(["__find_exec__" if w == "{}" else w for w in inner], cwd, res, depth + 1, prefixes=prefixes + ["find-exec"])
            i = j + 1
            continue
        if a == "-delete":
            res.writes.extend(str(p) for p in _expand(next((x for x in args if not x.startswith("-")), "."), cwd))
        i += 1
