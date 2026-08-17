"""chronos CLI: sync, watch, health, doctor."""

import argparse
import asyncio
import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import coverage, groups, query
from .store import GraphLocked, ensure_schema, open_driver
from .sync import Syncer, content_hash
from .upstream import UpstreamGraph, find_db


def _claim_group(args, repo=None) -> str:
    """The group to write into, with ownership asserted.

    Exits rather than merging when another repo owns the group: a wrong group
    silently invalidates the other repo's entire history, which is worse than
    a failed command. See groups.py for the incident this prevents.
    """
    repo = repo or getattr(args, "repo", None)
    group = groups.resolve(getattr(args, "group", None), repo)
    if repo:
        try:
            groups.claim(group, repo)
        except groups.GroupConflict as e:
            sys.exit(f"group conflict: {e}")
    return group


def _open_upstream(args) -> UpstreamGraph:
    p = Path(args.db) if args.db else find_db(repo=getattr(args, "repo", None))
    if not p or not Path(p).exists():
        where = f" for {args.repo}" if getattr(args, "repo", None) else ""
        sys.exit(f"no upstream SQLite graph found{where}. Run codebase-memory-mcp's "
                 "index_repository first, or pass --db /path/to/graph.db")
    g = UpstreamGraph(p)
    if not g.usable:
        sys.exit(f"could not map upstream schema.\n  {g.schema_report()}\n"
                 "Fix the column candidates in chronos/upstream.py.")
    return g


def _git(repo, *args, timeout=10):
    """Run a git command in repo. Returns stdout, or None on any failure."""
    try:
        out = subprocess.run(["git", "-C", repo, *args],
                             capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 -- git absent/broken must not break a sync
        return None


def commit_time(repo: str | None) -> datetime:
    """valid_at for a sync: HEAD's commit time, normalized to UTC.

    Two things this has to get right, both learned the hard way:

    1. **Always UTC.** git's %cI carries the committer's offset
       (2026-08-05T19:48:20+05:30). The graph stores timestamps naive, so
       handing it an offset-aware value kept the wall-clock digits and dropped
       the offset -- recording the fact 5h30m in the future and making every
       as-of query in that window wrong, silently. .astimezone(utc) converts
       instead of truncating.

    2. **A dirty tree is not HEAD.** Using HEAD's time unconditionally means
       every sync of uncommitted work stamps the same instant, so a fact
       created and superseded between two commits gets valid_at == invalid_at
       and is invisible to every as-of query -- the change's history is lost.
       When the tree is dirty the edit demonstrably is not part of HEAD, so we
       stamp now(). HEAD's time is still used for a clean tree, which is what
       keeps re-syncing an old checkout honest.
    """
    if repo:
        raw = _git(repo, "log", "-1", "--format=%cI")
        if raw and raw.strip():
            try:
                at = datetime.fromisoformat(raw.strip()).astimezone(timezone.utc)
            except ValueError:
                return datetime.now(timezone.utc)
            dirty = _git(repo, "status", "--porcelain")
            if dirty is not None and dirty.strip():
                # Never go backwards: on a dirty tree the working state is at
                # least as new as HEAD, and max() also survives a skewed clock.
                return max(at, datetime.now(timezone.utc))
            return at
    return datetime.now(timezone.utc)


async def do_sync(args):
    group = _claim_group(args)
    up = _open_upstream(args)
    nodes, edges = up.nodes(), up.edges()
    up.close()
    at = commit_time(args.repo)
    drv = open_driver()
    await ensure_schema(drv)
    t0 = time.time()
    st = await Syncer(drv, group).sync(nodes, edges, at)
    groups.log(group, "SYNC", f"{st}", args.repo or "")
    print(f"synced {group} @ {at.isoformat()} in {time.time()-t0:.1f}s: {st}")
    print(f"  content-hash {content_hash(nodes, edges)}")
    await drv.close()


async def do_health(args):
    # Read the same group writes go to, or health reports on a graph the repo
    # does not own -- the original BUG-1 shape, one layer down.
    group = groups.resolve(getattr(args, "group", None), args.repo)
    drv = open_driver()
    await ensure_schema(drv)
    h = await query.health(drv, group, find_db(repo=args.repo))
    print(json.dumps(h, indent=2))
    await drv.close()
    # non-zero exit on a graph you should not trust -- usable in CI/monitoring
    sys.exit(0 if h["status"] == "fresh" else 1)


async def do_watch(args):
    """Poll upstream's SQLite for changes and re-sync the delta (P0-3).

    ponytail: polling mtime+hash, not a filesystem watcher. Upstream already
    watches the repo; we only need to notice when its DB moved. One dependency
    fewer and it cannot miss an event.
    """
    print(f"watching (every {args.interval}s), ctrl-c to stop")
    last = None
    while True:
        try:
            up = _open_upstream(args)
            nodes, edges = up.nodes(), up.edges()
            up.close()
            h = content_hash(nodes, edges)
            if h != last:
                drv = open_driver()
                await ensure_schema(drv)
                st = await Syncer(drv, args.group).sync(nodes, edges, commit_time(args.repo))
                await drv.close()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] {h}: {st}")
                last = h
        except SystemExit:
            raise
        except Exception as e:
            print(f"[{datetime.now().strftime('%H:%M:%S')}] sync error: {e}", file=sys.stderr)
        await asyncio.sleep(args.interval)


async def do_index(args):
    """Index a repo with the vendored indexer, then sync it (P0-1 + P0-2 in one step)."""
    from .indexer import index_repo_graph
    repo = args.repo or "."
    # Claim the group BEFORE the indexer runs, for the same reason the graph is
    # claimed early: indexing is ~15s of subprocess work, and a group conflict
    # discovered afterwards would throw all of it away -- or worse, merge two
    # repos' histories. See groups.py.
    group = _claim_group(args, repo)
    drv = open_driver()
    t0 = time.time()
    nodes, edges = index_repo_graph(repo, mode=args.mode)
    print(f"indexed {repo}: {len(nodes)} nodes, {len(edges)} edges in {time.time()-t0:.1f}s")
    at = commit_time(repo)
    await ensure_schema(drv)
    st = await Syncer(drv, group).sync(nodes, edges, at)
    groups.log(group, "INDEX", f"{st}", repo)
    print(f"synced {group} @ {at.isoformat()}: {st}")

    # Coverage is computed here, at index time, and stored beside the index --
    # doctor reads the manifest rather than rescanning. A caller query is only
    # as trustworthy as the call graph behind it, so this number has to exist
    # before anyone asks as_of_callers a question.
    up = find_db(repo=repo)
    if up:
        cov = coverage.write_manifest(up, group, at.isoformat())
        c = cov.get("callable_coverage")
        if c is not None:
            print(f"  callable coverage {c:.0%} "
                  f"({cov['callable_with_callers']}/{cov['callable_total']} callables)")
        line = coverage.warning_line(cov)
        if line:
            print(f"  {line}")
    await drv.close()


def do_release_group(args):
    """Drop a group claim so a different repo can index into it."""
    if groups.release(args.group_id):
        print(f"released '{args.group_id}' -- another repo may now claim it")
    else:
        print(f"no claim on '{args.group_id}'")


def do_index_log(args):
    """Recent ingestions. SKIP rows are rejected writes -- see groups.py."""
    rows = groups.recent(30)
    if not rows:
        print("no ingestions recorded")
        return
    for r in rows:
        print(f"{r['ts'][:19]}  {r['outcome']:8} {r['group_id'][:34]:34} {r['reason'][:60]}")


async def do_gc(args):
    """Delete nodes whose facts have all been superseded (dry-run by default)."""
    group = groups.resolve(getattr(args, "group", None), getattr(args, "repo", None))
    drv = open_driver()
    await ensure_schema(drv)
    if not args.execute:
        o = await query.orphans(drv, group)
        print(f"{o['orphans']} orphaned nodes of {o['nodes_total']} ({o['pct']}%) in "
              f"group '{group}'")
        for s in o["sample"]:
            print(f"   would delete: {s['name']}  [{s['path'] or 'no path'}]")
        if o["orphans"] > len(o["sample"]):
            print(f"   ... and {o['orphans'] - len(o['sample'])} more")
        print("\ndry run -- nothing deleted. Re-run with --execute to delete."
              if o["orphans"] else "\nnothing to collect.")
    else:
        r = await query.collect_orphans(drv, group)
        print(f"deleted {r['deleted']} orphaned nodes from '{group}': "
              f"{r['nodes_before']} -> {r['nodes_after']} nodes")
    await drv.close()


CLAUDE_CONFIG = {
    "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
    "linux": "~/.config/Claude/claude_desktop_config.json",
}


def claude_desktop_config() -> Path | None:
    """Claude Desktop's config path for this OS, or None if it is not there."""
    import platform
    if os.name == "nt" or platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        p = Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None
    else:
        key = "darwin" if platform.system() == "Darwin" else "linux"
        p = Path(CLAUDE_CONFIG[key]).expanduser()
    return p if p and p.exists() else None


# Both hooks put --repo AFTER the subcommand. That only works because `index`
# and `enforce` each define their own --repo; the global one must precede the
# subcommand. `index` did not have one, so this post-merge hook exited 2 on
# every merge until it was added.
HOOKS = {
    "pre-commit": '#!/bin/sh\npython -m chronos enforce --repo "$(git rev-parse --show-toplevel)" --fail-on-block\n',
    "post-merge": '#!/bin/sh\npython -m chronos index --repo "$(git rev-parse --show-toplevel)"\n',
}


async def do_init(args):
    """Set Chronos up in a repo: data dir, config, MCP block, git hooks, doctor."""
    repo = Path(args.repo or ".").resolve()
    dot = repo / ".chronos"

    # 1. directories
    (dot / "rules").mkdir(parents=True, exist_ok=True)
    (dot / "logs").mkdir(parents=True, exist_ok=True)
    print(f"[1/6] created {dot}{os.sep}rules and {dot}{os.sep}logs")

    # 2. config
    packmind_set = bool(os.environ.get("PACKMIND_API_URL"))
    cfg = {
        "repo": str(repo),
        "chronos_sqlite": str(dot / "chronos.db"),
        "chronos_kuzu_path": str(dot / "graph"),
        "llm_model": "openrouter/anthropic/claude-3-haiku",
        "auto_triggers": "1",
        "rule_backend": "packmind" if packmind_set else "git-native",
    }
    (dot / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[2/6] wrote {dot / 'config.json'}")
    print("      Rule storage: git-native (default)")
    print("        Rules are proposed as draft PRs and stored in .chronos/rules/")
    if packmind_set:
        print("        PACKMIND_API_URL detected - Packmind path will be used.")
    else:
        print("        To enable org-wide Packmind instead:")
        print("          Set PACKMIND_API_URL and PACKMIND_API_KEY in the MCP env block")

    # 3/4. Claude Desktop MCP block
    cpath = claude_desktop_config()
    if cpath is None:
        print("[3/6] Claude Desktop config not found - skipping MCP registration")
        print("[4/6] skipped")
        mcp_written = None
    else:
        print(f"[3/6] found Claude Desktop config: {cpath}")
        try:
            doc = json.loads(cpath.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as e:
            print(f"[4/6] config is not valid JSON ({e}) - not touching it")
            doc, cpath = None, None
        if cpath is not None:
            servers = doc.setdefault("mcpServers", {})
            if "chronos" in servers:
                print("[4/6] MCP block already present, skipping.")
            else:
                servers["chronos"] = {
                    "command": "python",
                    "args": ["-m", "chronos.server"],
                    "env": {"CHRONOS_SQLITE": cfg["chronos_sqlite"],
                            "CHRONOS_KUZU_PATH": cfg["chronos_kuzu_path"],
                            "CHRONOS_AUTO_TRIGGERS": "1"},
                }
                # Only mcpServers.chronos is added; every other key round-trips.
                cpath.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                print(f"[4/6] wrote MCP block to {cpath}")
        mcp_written = cpath

    # 5. git hooks
    hooks_dir = repo / ".git" / "hooks"
    if not (repo / ".git").exists():
        print("[5/6] not a git repo - skipping hooks")
        hooks_ok = False
    else:
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for name, body in HOOKS.items():
            p = hooks_dir / name
            p.write_text(body, encoding="utf-8", newline="\n")  # sh needs LF
            p.chmod(p.stat().st_mode | 0o755)
        print(f"[5/6] installed {', '.join(HOOKS)} in {hooks_dir}")
        hooks_ok = True

    # 6. doctor
    print("[6/6] chronos doctor:")
    doctor_ok = True
    try:
        await do_doctor(args)
    except SystemExit as e:
        doctor_ok = not e.code
    except Exception as e:
        print(f"      doctor failed: {type(e).__name__}: {e}")
        doctor_ok = False

    # A Windows console defaults to cp1252, which cannot encode these glyphs --
    # printing them raises UnicodeEncodeError and takes the whole command down.
    try:
        "✓⚠✗".encode(sys.stdout.encoding or "utf-8")
        tick, warn, cross = "✓", "⚠", "✗"
    except (UnicodeEncodeError, LookupError):
        tick, warn, cross = "[ok]", "[!]", "[X]"
    print(f"\n{tick} .chronos/ created")
    print(f"{tick} MCP block written to {mcp_written}" if mcp_written
          else f"{warn} Claude Desktop config not found")
    print(f"{tick} git hooks installed" if hooks_ok else f"{warn} not a git repo")
    print(f"{tick} doctor passed" if doctor_ok else f"{cross} doctor failed - see above")
    print("\nNext: restart Claude Desktop to load the MCP server.")
    if hooks_ok:
        # The pre-commit hook is a fresh interpreter every commit, and the
        # import chain alone is ~5s. The daemon is the difference between a
        # hook developers keep and one they --no-verify around.
        print("Tip:  python -m chronos daemon start   "
              "-- pre-commit enforce ~300ms instead of ~5s")


def load_repo_config(repo) -> dict:
    """Config values from <repo>/.chronos/config.json.

    Environment always wins: an operator who exported CHRONOS_SQLITE meant it,
    and a stale config file should not silently override them."""
    # Publish the repo so db.db_path() resolves the same store the MCP tools do.
    # Without this the two entry points read different databases (see db.py).
    os.environ.setdefault("CHRONOS_REPO_PATH", str(Path(repo or ".").resolve()))
    p = Path(repo or ".").resolve() / ".chronos" / "config.json"
    if not p.exists():
        return {}
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    if cfg.get("chronos_sqlite") and not os.environ.get("CHRONOS_SQLITE"):
        os.environ["CHRONOS_SQLITE"] = cfg["chronos_sqlite"]
    if cfg.get("chronos_kuzu_path") and not os.environ.get("CHRONOS_DB"):
        # CHRONOS_KUZU_PATH is the config's name for what store.py reads as
        # CHRONOS_DB; both are honoured so neither spelling silently no-ops.
        os.environ["CHRONOS_DB"] = os.environ.get("CHRONOS_KUZU_PATH") or cfg["chronos_kuzu_path"]
    return cfg


async def do_enforce(args):
    """Run Wedge 4 rules over changed files; exit 1 on any block (CI gate).

    # CI usage:
    # - name: Chronos enforce
    #   run: python -m chronos enforce --repo . --exit-code
    #   env:
    #     CHRONOS_SQLITE: .chronos/chronos.db
    #     CHRONOS_KUZU_PATH: .chronos/graph
    """
    from . import enforcer, indexer, rule_store

    repo = args.repo or "."
    # Drain pytest failure traces before enforcing: CI is where test runs and
    # enforcement meet, so it is the natural moment to turn failures into
    # lessons. process_pending never raises.
    from .trace_processor import process_pending
    n = process_pending(repo)
    if n > 0:
        print(f"[Chronos] {n} trace(s) dispatched to Reflector", file=sys.stderr)
    load_repo_config(repo)  # env wins; config.json fills the gaps
    fail_on_block = args.fail_on_block or args.exit_code

    try:
        files = select_files(repo, args.file, args.diff)
    except ValueError as e:
        sys.exit(str(e))
    if not files:
        print("no changed files to check")
        return

    drv = open_driver()
    try:
        report = await enforce_files(files, repo, lang=args.lang, group=args.group,
                                     agent_id=args.agent_id, session_id=args.session_id,
                                     driver=drv)
    finally:
        await drv.close()

    print_enforce_report(report)
    if report["blocks"] and fail_on_block:
        sys.exit(1)


def select_files(repo, file=None, diff=None) -> list[str]:
    """Which files enforce should check: an explicit --file, else a git diff.

    Shared with the daemon so both paths agree on scope. Raises ValueError
    rather than exiting, since the daemon must return the error to its caller
    instead of killing the resident process.
    """
    if file:
        return [file]
    ref = diff or "HEAD~1"
    out = subprocess.run(["git", "-C", repo, "diff", "--name-only",
                          "--diff-filter=ACMR", ref],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise ValueError(f"git diff {ref} failed: {out.stderr.strip()}")
    # --diff-filter=ACMR already excludes deletions; the exists() check in
    # enforce_files also covers a file removed after the diff was taken.
    return [f for f in out.stdout.split() if f.strip()]


async def enforce_files(files, repo, lang=None, group="default",
                        agent_id=None, session_id=None, driver=None) -> dict:
    """Enforce over `files` and return the result as data, printing nothing.

    Split out of do_enforce so the daemon runs the SAME code as the direct
    path. Two implementations of "what is the verdict" would eventually
    disagree, and a gate that disagrees with itself depending on how it was
    invoked is worse than a slow one.
    """
    from . import enforcer, indexer

    await ensure_schema(driver)
    rows = []
    blocks = warns = oks = checked = skipped = 0
    for f in files:
        path = Path(repo, f)
        if not path.exists():
            continue  # deleted since the diff was taken
        # --lang applies to every file; without it, infer per file from the
        # extension so a mixed-language diff checks each file against its
        # own rules instead of one language's.
        flang = lang or indexer.node_language(f)
        if flang == "unknown":
            skipped += 1
            continue
        checked += 1
        results = await enforcer.enforce(str(path), flang, agent_id=agent_id,
                                         session_id=session_id,
                                         driver=driver, group_id=group)
        hits = [r for r in results if r["verdict"] != "pass"]
        if not hits:
            oks += 1
            rows.append({"file": f, "verdict": "ok"})
            continue
        for r in hits:
            blocks += r["verdict"] == "block"
            warns += r["verdict"] == "warn"
            rows.append({"file": f, "verdict": r["verdict"], "line": _line_of(r),
                         "rule_id": r["rule_id"], "message": (r["message"] or "").strip()})
    return {"rows": rows, "blocks": blocks, "warns": warns, "oks": oks,
            "checked": checked, "skipped": skipped}


def print_enforce_report(report: dict):
    """Render an enforce report. Identical output from daemon and direct paths."""
    for row in report["rows"]:
        if row["verdict"] == "ok":
            print(f'OK     {row["file"]}')
            continue
        loc = f'{row["file"]}:{row["line"]}' if row.get("line") else row["file"]
        print(f'{row["verdict"].upper():<6} {loc}  rule:{row["rule_id"]}  '
              f'"{row["message"]}"')
    # Report skips: "Checked 0 files" on a 26-file diff is indistinguishable
    # from a broken enforce unless it says why nothing was checked.
    tail = (f' ({report["skipped"]} skipped: no rules for that file type)'
            if report["skipped"] else "")
    print(f'Checked {report["checked"]} files - {report["blocks"]} block, '
          f'{report["warns"]} warn, {report["oks"]} ok{tail}')


def _line_of(result) -> str:
    """Line number out of the enforcer's message, which ends in [file:line]."""
    msg = result.get("message") or ""
    if msg.endswith("]") and ":" in msg:
        tail = msg.rsplit("[", 1)[-1].rstrip("]")
        if ":" in tail and tail.rsplit(":", 1)[-1].isdigit():
            return tail.rsplit(":", 1)[-1]
    return ""


def _daemon_state() -> dict | None:
    from .daemon import protocol
    try:
        return json.loads(protocol.state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def do_daemon(args):
    """start | stop | status for the resident process."""
    from .daemon import protocol
    from .daemon.client import DaemonClient

    verb = args.verb
    client = DaemonClient()

    if verb == "status":
        info = client.ping()
        if info is None:
            reason = (f"disabled ({protocol.DISABLE_ENV}=0)"
                      if protocol.daemon_disabled() else "not running")
            print(f"Daemon: {reason} -- start it with: python -m chronos daemon start")
            return
        state = _daemon_state() or {}
        up = time.time() - state.get("started_at", time.time())
        print(f"Daemon: running -- pid {info['pid']}, port {client.port}, "
              f"up {up:.0f}s, {info.get('served', 0)} requests served")
        return

    if verb == "stop":
        if not client.available():
            print("No daemon running.")
            return
        pid = (client.ping() or {}).get("pid")
        if not client.shutdown():
            print("Daemon did not acknowledge shutdown; it may already be gone.")
            return
        # The daemon answers shutdown before leaving its accept loop, so the
        # state file can outlive this reply by up to a second. Wait for it, or
        # the next command reads a file pointing at a dying process.
        sf = protocol.state_file()
        deadline = time.time() + 10
        while sf.exists() and time.time() < deadline:
            time.sleep(0.1)
        print(f"Daemon stopped (pid {pid}).")
        if sf.exists():
            print("  (state file still present -- the daemon may be finishing a request)")
        return

    # start
    if client.available():
        info = client.ping() or {}
        print(f"Daemon already running (pid {info.get('pid')}, port {client.port}).")
        return
    if protocol.daemon_disabled():
        print(f"{protocol.DISABLE_ENV} is set to off -- refusing to start. "
              f"Unset it first.")
        raise SystemExit(1)

    # A stale state file from a killed daemon makes `available()` false but
    # would confuse the next reader; clear it before spawning.
    sf = protocol.state_file()
    if sf.exists():
        try:
            sf.unlink()
        except OSError:
            pass

    cmd = [sys.executable, "-m", "chronos.daemon.server"]
    kw = {}
    if sys.platform == "win32":
        # Detach so the daemon outlives this shell, and give it no console.
        kw["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=os.environ.copy(), **kw)

    # Wait for the ready line rather than sleeping: startup is dominated by the
    # import chain and varies with disk cache, so any fixed sleep is either a
    # stall or a race.
    deadline = time.time() + 60
    line = ""
    while time.time() < deadline:
        if proc.poll() is not None:
            err = (proc.stderr.read() or "").strip() if proc.stderr else ""
            print(f"Daemon exited during startup (code {proc.returncode}).")
            if err:
                print(err[-1500:], file=sys.stderr)
            raise SystemExit(1)
        line = proc.stdout.readline() if proc.stdout else ""
        if line.startswith(protocol.READY_PREFIX):
            break
        if not line:
            time.sleep(0.05)
    else:
        print("Daemon did not report ready within 60s; leaving it running. "
              "Check: python -m chronos daemon status")
        raise SystemExit(1)

    parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
    print(f"Chronos daemon started (pid {parts.get('pid')}, port {parts.get('port')}).")
    print("  enforce/index now run against the warm process (~300ms vs ~5s).")
    print("  Stop with: python -m chronos daemon stop")


def do_dashboard(args):
    """Serve the read-only developer dashboard.

    Deliberately NOT async: uvicorn.run() starts its own event loop, and calling
    it from inside asyncio.run() raises "cannot be called from a running event
    loop". main() dispatches this one synchronously.
    """
    from .dashboard_server import serve
    serve(host=args.host, port=args.port)


def _branch_merged(repo_path, branch, base):
    """True if `branch` is merged into `base`. None if git can't tell us."""
    r = subprocess.run(["git", "-C", str(repo_path), "branch", "--merged", base],
                       check=False, capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return any(line.strip().lstrip("* ") == branch for line in r.stdout.splitlines())


async def do_approve_rule(args):
    """Git-native approval: proposed -> warn-only-unvalidated."""
    from . import rule_store
    from .rule_submission import BRANCH_PREFIX, resolve_repo_path

    rule_id = args.rule_id
    rule = rule_store.get_rule(rule_id)
    if rule is None:
        print(f"No such rule: {rule_id}")
        raise SystemExit(1)
    if rule["status"] != rule_store.PROPOSED:
        print(f"Rule is already past proposed state (current: {rule['status']}). "
              f"Use promote-rule to advance it further.")
        raise SystemExit(0)

    repo_path = resolve_repo_path(getattr(args, "repo_sub", None))
    branch = f"{BRANCH_PREFIX}{rule_id}"
    merged = None
    for base in ("main", "master"):
        merged = _branch_merged(repo_path, branch, base)
        if merged:
            break
    if merged:
        print("[OK] Branch merged - rule approved and moved to warn-only.")
    else:
        print("[!] Branch not yet merged. Approving locally anyway. "
              "Rule will enforce in warn-only mode from this machine.")

    r = rule_store.approve_rule(rule_id)
    if not r["approved"]:
        print(f"Could not approve: {r['reason']}")
        raise SystemExit(1)
    print(f"Rule {rule_id} approved.")
    print(f"Status: {rule_store.PROPOSED} -> {rule_store.UNVALIDATED}")
    print(f"Run 'python -m chronos promote-rule {rule_id}' when validated "
          f"to make it blocking.")


async def do_promote_rule(args):
    """Human promotion to blocking. Refuses anything not yet validated."""
    from . import rule_store
    r = rule_store.promote_to_blocking(args.rule_id, args.by)
    if not r["promoted"]:
        print(f"Not promoted: {r['reason']}")
        raise SystemExit(1)
    print(f"Rule {args.rule_id} promoted to {r['status']} by {r['promoted_by']}.")


def _fake_packmind_roundtrip():
    """Exercise the Packmind HTTP layer against tests/fake_packmind.py.

    Verifies our client (URLs, bearer header, body shapes, evidence
    round-trip) on any machine, with no credentials and no Docker. It does NOT
    verify that the real Packmind agrees with our reading of its source —
    only a live run does that. Exit 0 on PASS, 1 on FAIL."""
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    try:
        import fake_packmind
    except ImportError:
        print("fake packmind: MISSING -- tests/fake_packmind.py not found")
        return 1
    from .playbook import Packmind

    httpd, port = fake_packmind.serve(port=0)
    base = f"http://127.0.0.1:{port}"
    try:
        pm = Packmind(url=base, key="fake")
        evidence = {"evidence_node": "demo::fn", "evidence_valid_at": "2026-01-01T00:00:00+00:00",
                    "source": "chronos-doctor", "status": "proposed"}
        sid = pm.create_standard("IF x THEN y, for a reason.", evidence)
        back = pm.list_rules()
        ok = (sid == "fake-rule-001" and len(back) == 1
              and back[0]["evidence_node"] == "demo::fn"
              and back[0]["rule_text"].startswith("IF x THEN y"))
        print(f"fake packmind: {'PASS' if ok else 'FAIL'} | created {sid} | "
              f"read back {len(back)} rule(s) with evidence intact")
        if not ok:
            print(f"              got: {back}")
        return 0 if ok else 1
    except Exception as e:
        print(f"fake packmind: FAIL {type(e).__name__}: {e}")
        return 1
    finally:
        httpd.shutdown()


async def do_doctor(args):
    if getattr(args, "fake_packmind", False):
        raise SystemExit(_fake_packmind_roundtrip())
    from .indexer import toolchain_report
    t = toolchain_report()
    print(f"vendored src: {'present' if t['vendored'] else 'MISSING -- git submodule update --init --depth 1'}")
    if t["binary"]:
        print(f"indexer     : {t['binary']}")
    else:
        print(f"indexer     : NOT BUILT -- run: {t['build_cmd']}")
        print(f"              toolchain: make={t['make'] or 'MISSING'} cc={t['cc'] or 'MISSING'}")
    p = Path(args.db) if args.db else find_db(repo=args.repo)
    if p:
        print(f"upstream db : {p}")
    elif args.repo:
        # Naming the repo is the point: "NOT FOUND" alone used to be impossible
        # to distinguish from "found someone else's index".
        print(f"upstream db : NOT INDEXED -- run: chronos --repo {args.repo} index")
    else:
        print("upstream db : NOT FOUND -- pass --repo <path> or --db <file>")
    if p and Path(p).exists():
        g = UpstreamGraph(p)
        print(f"schema      : {g.schema_report()}")
        if g.usable:
            n, e = g.nodes(), g.edges()
            print(f"upstream    : {len(n)} nodes, {len(e)} temporal edges")
        g.close()
        # Read the manifest written at index time; fall back to computing it so
        # an index built before coverage tracking still reports something.
        cov = coverage.read_manifest(p) or coverage.compute(p)
        c = cov.get("callable_coverage")
        if c is None:
            print(f"coverage    : unknown ({cov.get('reason', 'not computed')})")
        else:
            print(f"coverage    : {c:.0%} call-graph "
                  f"({cov.get('callable_with_callers')}/{cov.get('callable_total')} callables)")
            line = coverage.warning_line(cov)
            if line:
                print(f"              {line}")
    # Doctor must keep working when the graph is held by the daemon -- a
    # diagnostic that dies on the condition you are diagnosing is useless. Every
    # other line below still prints.
    try:
        drv = open_driver()
    except GraphLocked:
        drv = None
        print("chronos     : LOCKED by another process (the daemon holds it) -- "
              "graph stats unavailable here; use: python -m chronos daemon status")
    if drv is not None:
        try:
            await ensure_schema(drv)
            grp = groups.resolve(getattr(args, "group", None), getattr(args, "repo", None))
            h = await query.health(drv, grp)
            print(f"chronos     : {h['status']} | {h['nodes']} nodes | "
                  f"{h['facts_current']}/{h['facts_total']} facts current | last {h['last_sync']}")
            print(f"group       : {grp}")
            o = await query.orphans(drv, grp, sample=0)
            if o["pct"] > 10:
                print(f"              WARNING: {o['orphans']} orphaned nodes ({o['pct']}% of total) "
                      f"-- run: chronos --group {grp} gc")
        finally:
            await drv.close()

    # Rejected ingestions are only useful if someone can see them. A SKIP that
    # nobody reads is the silent merge it was meant to replace.
    try:
        skips = [r for r in groups.recent(50) if r["outcome"] == "SKIP"]
        if skips:
            print(f"index log   : {len(skips)} REJECTED ingestion(s) -- group conflicts")
            for r in skips[:3]:
                print(f"              {r['ts'][:19]}  {r['reason'][:88]}")
        else:
            print("index log   : ok | no rejected ingestions")
    except Exception as e:
        print(f"index log   : unavailable ({type(e).__name__})")

    # Unification: one line for one database. Locks, provenance and enforcement
    # rules share chronos.db, so a partner has a single path to back up.
    from . import db as _db
    from . import ledger
    try:
        with contextlib.closing(ledger.connect()) as lc:
            s = ledger.status(lc)
            rules = lc.execute("SELECT count(*) c FROM enforcement_rules").fetchone()["c"]
        p = _db.db_path()
        mb = p.stat().st_size / 1048576 if p.exists() else 0.0
        print(f"database    : {p} | {s['active_locks']} locks | {s['events']} events | "
              f"{rules} rules | {mb:.1f}MB")
    except Exception as e:
        print(f"database    : ERROR {e}")

    # Wedge 2. Unconfigured is a normal state (Packmind is optional), so say so
    # plainly rather than reporting it as an error.
    from .playbook import Packmind, PackmindError
    try:
        h = Packmind().health()
        if h["status"] == "ok":
            print(f"packmind    : ok | {h['total']} rules | last proposal {h['last_proposal'] or 'none'}")
        else:
            print(f"packmind    : UNREACHABLE {h.get('error')}")
    except PackmindError as e:
        print(f"packmind    : not configured ({e})")

    # Daemon. Purely a latency question -- everything works without it -- so a
    # missing daemon is reported as a tip, never as an error.
    try:
        from .daemon import protocol as _dp
        from .daemon.client import DaemonClient as _DC
        _c = _DC()
        _i = _c.ping()
        if _i:
            _s = _daemon_state() or {}
            _up = time.time() - _s.get("started_at", time.time())
            print(f"daemon      : running | pid {_i['pid']} | port {_c.port} | up {_up:.0f}s")
        elif _dp.daemon_disabled():
            print(f"daemon      : disabled ({_dp.DISABLE_ENV} is off)")
        else:
            print("daemon      : not running -- start: python -m chronos daemon start "
                  "(enforce ~300ms vs ~5s)")
    except Exception as e:  # noqa: BLE001 -- diagnostics must never abort doctor
        print(f"daemon      : UNKNOWN ({type(e).__name__})")

    # Wedge 2 input path. The pytest plugin is registered by an entry point, and
    # an editable install does NOT regenerate entry_points.txt when pyproject
    # changes -- so capture can be silently dead while every test still passes.
    # Chronos's own repo hides this behind a conftest.py, which is exactly why
    # it went unnoticed; partner repos have no such fallback.
    try:
        import importlib.metadata as _md
        live = any(e.name == "chronos" for e in _md.entry_points(group="pytest11"))
        print(f"capture     : {'ok | pytest plugin registered' if live else 'INACTIVE -- pytest traces are NOT being captured. Fix: pip install -e .'}")
    except Exception as e:  # noqa: BLE001 -- diagnostics must never abort doctor
        print(f"capture     : UNKNOWN ({type(e).__name__})")

    # Wedge 4. Both binaries are external and required for enforcement to mean
    # anything, so a missing one is reported with its install command.
    from . import enforcer, rule_store
    sgv = enforcer.ast_grep_version()
    print(f"ast-grep    : {'ok | ' + sgv if sgv else 'MISSING -- install: pip install ast-grep-cli'}")
    opav = enforcer.opa_version()
    print(f"opa         : {'ok | v' + opav if opav else 'MISSING -- see docs/wedge4-ci.yml'}")
    try:
        # Rule totals live on the `database` line above; this reports the split
        # that actually changes behaviour -- how many rules can block a merge.
        c = rule_store.counts()
        print(f"enforce     : ok | {c['blocking']} blocking, {c['warn_only']} warn-only")
    except Exception as e:
        print(f"enforce     : ERROR {e}")


def main():
    ap = argparse.ArgumentParser(prog="chronos", description="bi-temporal AST knowledge graph")
    ap.add_argument("--group", default="default", help="repo/group id")
    ap.add_argument("--db", help="path to upstream codebase-memory-mcp sqlite db")
    ap.add_argument("--repo", help="repo path, for git commit timestamps")
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("index", help="index a repo with the vendored indexer, then sync")
    i.add_argument("--mode", default="fast", choices=["fast", "moderate", "full"])
    # Accepted after the subcommand too, matching enforce/init. The post-merge
    # hook init writes uses this position; without it the hook exits 2 on every
    # merge and the repo silently stops re-indexing.
    i.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    sy = sub.add_parser("sync", help="one-shot sync into the temporal graph")
    sy.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    w = sub.add_parser("watch", help="continuously sync on change")
    w.add_argument("--interval", type=int, default=30)
    w.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    he = sub.add_parser("health", help="index health (exit 1 if not fresh)")
    he.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    doc = sub.add_parser("doctor", help="diagnose upstream + chronos wiring")
    doc.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    doc.add_argument("--fake-packmind", action="store_true",
                     help="verify the Packmind HTTP layer against a local fake "
                          "(no credentials, no Docker); exit 1 on failure")
    rg = sub.add_parser("release-group",
                        help="release a group claim so another repo can use it")
    rg.add_argument("group_id", help="group id to release")
    sub.add_parser("index-log", help="recent ingestions, including rejected ones")
    gc = sub.add_parser("gc", help="delete nodes whose facts are all superseded")
    gc.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
    apr = sub.add_parser("approve-rule",
                         help="approve a git-native proposed rule (-> warn-only)")
    apr.add_argument("rule_id")
    apr.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    pro = sub.add_parser("promote-rule",
                         help="promote a validated rule to blocking")
    pro.add_argument("rule_id")
    pro.add_argument("--by", default=os.environ.get("USER") or "unknown",
                     help="who is promoting (recorded in the ledger)")
    dash = sub.add_parser("dashboard", help="serve the developer dashboard")
    dash.add_argument("--port", type=int, default=8080)
    dash.add_argument("--host", default="127.0.0.1")
    ini = sub.add_parser("init", help="set Chronos up in a repo (dirs, config, MCP, hooks)")
    # --repo is also a global flag, but `chronos init --repo X` is the natural
    # order (and what the generated git hooks use), so accept it here too.
    ini.add_argument("--repo", dest="repo_sub", help="repo to set up (default: cwd)")
    en = sub.add_parser("enforce", help="run Wedge 4 CI rules over changed files")
    en.add_argument("--repo", dest="repo_sub", help="repo root (default: cwd)")
    en.add_argument("--diff", help="git ref to diff against (default HEAD~1)")
    en.add_argument("--file", help="check a single file instead of a diff")
    en.add_argument("--lang", help="language, e.g. typescript "
                    "(default: inferred per file from its extension)")
    en.add_argument("--fail-on-block", action="store_true",
                    help="exit 1 if any rule blocks (use in CI)")
    en.add_argument("--exit-code", action="store_true",
                    help="alias for --fail-on-block")
    en.add_argument("--agent-id", help="stamp blocks into the provenance ledger")
    en.add_argument("--session-id")
    dmn = sub.add_parser("daemon", help="resident process that keeps the graph warm")
    dmn.add_argument("verb", choices=["start", "stop", "status"])
    args = ap.parse_args()
    # subcommand --repo wins over the global one when both are given
    if getattr(args, "repo_sub", None):
        args.repo = args.repo_sub
    fn = {"index": do_index, "sync": do_sync, "watch": do_watch,
          "health": do_health, "doctor": do_doctor, "gc": do_gc,
          "enforce": do_enforce, "init": do_init,
          "approve-rule": do_approve_rule, "promote-rule": do_promote_rule,
          "dashboard": do_dashboard, "daemon": do_daemon,
          "release-group": do_release_group, "index-log": do_index_log}[args.cmd]
    try:
        # dashboard and daemon are sync (uvicorn owns its loop; daemon control
        # is plain socket I/O); everything else is a coroutine
        if args.cmd in ("dashboard", "daemon", "release-group", "index-log"):
            fn(args)
        else:
            asyncio.run(fn(args))
    except KeyboardInterrupt:
        pass
    except GraphLocked as e:
        # One process may hold the embedded graph. Print the remedy instead of
        # a kuzu traceback -- with a daemon running this is the single most
        # likely error a developer hits.
        sys.exit(f"chronos: {e}")


if __name__ == "__main__":
    main()
