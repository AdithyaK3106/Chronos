"""chronos CLI: sync, watch, health, doctor."""

import argparse
import asyncio
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
    await drv.close()


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
    args = ap.parse_args()
    fn = {"index": do_index, "sync": do_sync, "watch": do_watch,
          "health": do_health, "doctor": do_doctor}[args.cmd]
    try:
        asyncio.run(fn(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
