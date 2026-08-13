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

from . import query
from .store import ensure_schema, open_driver
from .sync import Syncer, content_hash
from .upstream import UpstreamGraph, find_db


def _open_upstream(args) -> UpstreamGraph:
    p = Path(args.db) if args.db else find_db()
    if not p or not Path(p).exists():
        sys.exit("no upstream SQLite graph found. Run codebase-memory-mcp's "
                 "index_repository first, or pass --db /path/to/graph.db")
    g = UpstreamGraph(p)
    if not g.usable:
        sys.exit(f"could not map upstream schema.\n  {g.schema_report()}\n"
                 "Fix the column candidates in chronos/upstream.py.")
    return g


def commit_time(repo: str | None) -> datetime:
    """Use HEAD's commit time as valid_at so history matches the repo, not the
    clock -- re-syncing an old checkout must not claim it is current."""
    if repo:
        try:
            out = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%cI"],
                                 capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout.strip():
                return datetime.fromisoformat(out.stdout.strip())
        except Exception:
            pass
    return datetime.now(timezone.utc)


async def do_sync(args):
    up = _open_upstream(args)
    nodes, edges = up.nodes(), up.edges()
    up.close()
    at = commit_time(args.repo)
    drv = open_driver()
    await ensure_schema(drv)
    t0 = time.time()
    st = await Syncer(drv, args.group).sync(nodes, edges, at)
    print(f"synced {args.group} @ {at.isoformat()} in {time.time()-t0:.1f}s: {st}")
    print(f"  content-hash {content_hash(nodes, edges)}")
    await drv.close()


async def do_health(args):
    drv = open_driver()
    await ensure_schema(drv)
    h = await query.health(drv, args.group, find_db())
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
    t0 = time.time()
    nodes, edges = index_repo_graph(repo, mode=args.mode)
    print(f"indexed {repo}: {len(nodes)} nodes, {len(edges)} edges in {time.time()-t0:.1f}s")
    at = commit_time(repo)
    drv = open_driver()
    await ensure_schema(drv)
    st = await Syncer(drv, args.group).sync(nodes, edges, at)
    print(f"synced {args.group} @ {at.isoformat()}: {st}")
    await drv.close()


async def do_gc(args):
    """Delete nodes whose facts have all been superseded (dry-run by default)."""
    drv = open_driver()
    await ensure_schema(drv)
    if not args.execute:
        o = await query.orphans(drv, args.group)
        print(f"{o['orphans']} orphaned nodes of {o['nodes_total']} ({o['pct']}%) in "
              f"group '{args.group}'")
        for s in o["sample"]:
            print(f"   would delete: {s['name']}  [{s['path'] or 'no path'}]")
        if o["orphans"] > len(o["sample"]):
            print(f"   ... and {o['orphans'] - len(o['sample'])} more")
        print("\ndry run -- nothing deleted. Re-run with --execute to delete."
              if o["orphans"] else "\nnothing to collect.")
    else:
        r = await query.collect_orphans(drv, args.group)
        print(f"deleted {r['deleted']} orphaned nodes from '{args.group}': "
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
    cfg = {
        "repo": str(repo),
        "chronos_sqlite": str(dot / "chronos.db"),
        "chronos_kuzu_path": str(dot / "graph"),
        "llm_model": "openrouter/anthropic/claude-3-haiku",
        "auto_triggers": "1",
    }
    (dot / "config.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"[2/6] wrote {dot / 'config.json'}")

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


def load_repo_config(repo) -> dict:
    """Config values from <repo>/.chronos/config.json.

    Environment always wins: an operator who exported CHRONOS_SQLITE meant it,
    and a stale config file should not silently override them."""
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
    load_repo_config(repo)  # env wins; config.json fills the gaps
    fail_on_block = args.fail_on_block or args.exit_code

    if args.file:
        files = [args.file]
    else:
        ref = args.diff or "HEAD~1"
        out = subprocess.run(["git", "-C", repo, "diff", "--name-only",
                              "--diff-filter=ACMR", ref],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            sys.exit(f"git diff {ref} failed: {out.stderr.strip()}")
        # --diff-filter=ACMR already excludes deletions; the exists() check below
        # also covers a file removed after the diff was taken.
        files = [f for f in out.stdout.split() if f.strip()]
    if not files:
        print("no changed files to check")
        return

    drv = open_driver()
    await ensure_schema(drv)
    blocks = warns = oks = checked = 0
    try:
        for f in files:
            path = Path(repo, f)
            if not path.exists():
                continue  # deleted since the diff was taken
            # --lang applies to every file; without it, infer per file from the
            # extension so a mixed-language diff checks each file against its
            # own rules instead of one language's.
            lang = args.lang or indexer.node_language(f)
            if lang == "unknown":
                continue
            checked += 1
            results = await enforcer.enforce(str(path), lang,
                                             agent_id=args.agent_id,
                                             session_id=args.session_id,
                                             driver=drv, group_id=args.group)
            hits = [r for r in results if r["verdict"] != "pass"]
            if not hits:
                oks += 1
                print(f"OK     {f}")
                continue
            for r in hits:
                line = _line_of(r)
                loc = f"{f}:{line}" if line else f
                blocks += r["verdict"] == "block"
                warns += r["verdict"] == "warn"
                print(f'{r["verdict"].upper():<6} {loc}  rule:{r["rule_id"]}  '
                      f'"{(r["message"] or "").strip()}"')
    finally:
        await drv.close()

    print(f"Checked {checked} files - {blocks} block, {warns} warn, {oks} ok")
    if blocks and fail_on_block:
        sys.exit(1)


def _line_of(result) -> str:
    """Line number out of the enforcer's message, which ends in [file:line]."""
    msg = result.get("message") or ""
    if msg.endswith("]") and ":" in msg:
        tail = msg.rsplit("[", 1)[-1].rstrip("]")
        if ":" in tail and tail.rsplit(":", 1)[-1].isdigit():
            return tail.rsplit(":", 1)[-1]
    return ""


def do_dashboard(args):
    """Serve the read-only developer dashboard.

    Deliberately NOT async: uvicorn.run() starts its own event loop, and calling
    it from inside asyncio.run() raises "cannot be called from a running event
    loop". main() dispatches this one synchronously.
    """
    from .dashboard_server import serve
    serve(host=args.host, port=args.port)


async def do_doctor(args):
    from .indexer import toolchain_report
    t = toolchain_report()
    print(f"vendored src: {'present' if t['vendored'] else 'MISSING -- git submodule update --init --depth 1'}")
    if t["binary"]:
        print(f"indexer     : {t['binary']}")
    else:
        print(f"indexer     : NOT BUILT -- run: {t['build_cmd']}")
        print(f"              toolchain: make={t['make'] or 'MISSING'} cc={t['cc'] or 'MISSING'}")
    p = Path(args.db) if args.db else find_db()
    print(f"upstream db : {p or 'NOT FOUND'}")
    if p and Path(p).exists():
        g = UpstreamGraph(p)
        print(f"schema      : {g.schema_report()}")
        if g.usable:
            n, e = g.nodes(), g.edges()
            print(f"upstream    : {len(n)} nodes, {len(e)} temporal edges")
        g.close()
    drv = open_driver()
    await ensure_schema(drv)
    h = await query.health(drv, args.group)
    print(f"chronos     : {h['status']} | {h['nodes']} nodes | "
          f"{h['facts_current']}/{h['facts_total']} facts current | last {h['last_sync']}")
    o = await query.orphans(drv, args.group, sample=0)
    if o["pct"] > 10:
        print(f"              WARNING: {o['orphans']} orphaned nodes ({o['pct']}% of total) "
              f"-- run: chronos --group {args.group} gc")
    await drv.close()

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
    sub.add_parser("sync", help="one-shot sync into the temporal graph")
    w = sub.add_parser("watch", help="continuously sync on change")
    w.add_argument("--interval", type=int, default=30)
    sub.add_parser("health", help="index health (exit 1 if not fresh)")
    sub.add_parser("doctor", help="diagnose upstream + chronos wiring")
    gc = sub.add_parser("gc", help="delete nodes whose facts are all superseded")
    gc.add_argument("--execute", action="store_true", help="actually delete (default: dry run)")
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
    args = ap.parse_args()
    # subcommand --repo wins over the global one when both are given
    if getattr(args, "repo_sub", None):
        args.repo = args.repo_sub
    fn = {"index": do_index, "sync": do_sync, "watch": do_watch,
          "health": do_health, "doctor": do_doctor, "gc": do_gc,
          "enforce": do_enforce, "init": do_init,
          "dashboard": do_dashboard}[args.cmd]
    try:
        # dashboard runs its own loop (uvicorn); everything else is a coroutine
        if args.cmd == "dashboard":
            fn(args)
        else:
            asyncio.run(fn(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
