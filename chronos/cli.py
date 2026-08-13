"""chronos CLI: sync, watch, health, doctor."""

import argparse
import asyncio
import contextlib
import json
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


async def do_enforce(args):
    """Run Wedge 4 rules over changed files; exit 1 on any block (CI gate)."""
    from . import enforcer, indexer, rule_store

    if args.file:
        files = [args.file]
    else:
        ref = args.diff or "HEAD~1"
        out = subprocess.run(["git", "-C", args.repo or ".", "diff", "--name-only",
                              "--diff-filter=ACMR", ref],
                             capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            sys.exit(f"git diff {ref} failed: {out.stderr.strip()}")
        files = [f for f in out.stdout.split() if f.strip()]
    if not files:
        print("no changed files to check")
        return

    drv = open_driver()
    await ensure_schema(drv)
    rows, blocked = [], 0
    try:
        for f in files:
            if not Path(args.repo or ".", f).exists():
                continue
            # --lang applies to every file; without it, infer per file from the
            # extension so a mixed-language diff checks each file against its
            # own rules instead of one language's.
            lang = args.lang or indexer.node_language(f)
            if lang == "unknown":
                continue
            for r in await enforcer.enforce(str(Path(args.repo or ".", f)), lang,
                                            agent_id=args.agent_id,
                                            session_id=args.session_id,
                                            driver=drv, group_id=args.group):
                if r["verdict"] == "pass":
                    continue
                blocked += r["verdict"] == "block"
                rows.append((r["verdict"].upper(), r["rule_id"], f,
                             (r["matched_qualified_name"] or "-"), r["message"]))
    finally:
        await drv.close()

    if not rows:
        n = rule_store.counts()["total"]
        print(f"chronos enforce: {len(files)} file(s), {n} rule(s) — no violations")
    else:
        w = max(len(r[1]) for r in rows)
        for v, rid, f, node, msg in rows:
            print(f"{v:<5} {rid:<{w}}  {f}  {node}\n      {msg}")
        print(f"\n{len(rows)} violation(s): {blocked} block, {len(rows)-blocked} warn")
    if blocked and args.fail_on_block:
        sys.exit(1)


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
    en = sub.add_parser("enforce", help="run Wedge 4 CI rules over changed files")
    en.add_argument("--diff", help="git ref to diff against (default HEAD~1)")
    en.add_argument("--file", help="check a single file instead of a diff")
    en.add_argument("--lang", help="language, e.g. typescript "
                    "(default: inferred per file from its extension)")
    en.add_argument("--fail-on-block", action="store_true",
                    help="exit 1 if any rule blocks (use in CI)")
    en.add_argument("--agent-id", help="stamp blocks into the provenance ledger")
    en.add_argument("--session-id")
    args = ap.parse_args()
    fn = {"index": do_index, "sync": do_sync, "watch": do_watch,
          "health": do_health, "doctor": do_doctor, "gc": do_gc,
          "enforce": do_enforce}[args.cmd]
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
