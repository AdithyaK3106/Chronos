"""Chronos command line interface."""

import argparse
import os
import sys
from pathlib import Path

from chronos.audit.logger import verify_audit_chain
from chronos.context.graph import SQLiteGraphStore
from chronos.context.indexer import index_repository
from chronos.core.config import ChronosConfig
from chronos.core.db import get_connection

def main():
    parser = argparse.ArgumentParser(description="Chronos: Bi-temporal intelligence and AI governance")
    subparsers = parser.add_subparsers(dest="command")

    # init
    p_init = subparsers.add_parser("init", help="Initialize .chronos directory in current repository")
    p_init.add_argument("--repo", default=".", help="Repository root path")

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Check database and toolchain health")
    p_doc.add_argument("--repo", default=".", help="Repository root path")

    # sync
    p_sync = subparsers.add_parser("sync", help="Index repository AST into bi-temporal graph")
    p_sync.add_argument("--repo", default=".", help="Repository root path")

    # audit verify
    p_audit = subparsers.add_parser("audit", help="Audit log commands")
    p_audit.add_argument("subcommand", choices=["verify"], help="Subcommand (verify)")
    p_audit.add_argument("--repo", default=".", help="Repository root path")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    repo_root = Path(getattr(args, "repo", ".")).resolve()
    chronos_dir = repo_root / ".chronos"
    db_file = chronos_dir / "chronos.db"

    if args.command == "init":
        chronos_dir.mkdir(parents=True, exist_ok=True)
        rules_dir = chronos_dir / "rules"
        rules_dir.mkdir(parents=True, exist_ok=True)
        con = get_connection(db_file)
        con.close()
        print(f"Initialized Chronos in {chronos_dir}")
        sys.exit(0)

    elif args.command == "doctor":
        status_ok = True
        print("Chronos Doctor Report:")
        print("---------------------")
        print(f"Repository Root : {repo_root}")
        print(f"Chronos Dir     : {'OK' if chronos_dir.is_dir() else 'Missing (.chronos/)'}")
        
        try:
            con = get_connection(db_file)
            row = con.execute("SELECT count(*) as c FROM sqlite_master WHERE type='table'").fetchone()
            print(f"SQLite WAL DB   : OK ({row['c']} tables)")
            con.close()
        except Exception as e:
            print(f"SQLite WAL DB   : FAILED ({e})")
            status_ok = False

        try:
            import tree_sitter
            print("Tree-Sitter     : OK")
        except ImportError:
            print("Tree-Sitter     : Missing")
            status_ok = False

        try:
            import openhands.sdk
            print("OpenHands SDK   : OK (v1.58.2)")
        except ImportError:
            print("OpenHands SDK   : Missing")
            status_ok = False

        sys.exit(0 if status_ok else 1)

    elif args.command == "sync":
        con = get_connection(db_file)
        store = SQLiteGraphStore(con)
        print(f"Indexing repository {repo_root}...")
        nodes, edges = index_repository(repo_root)
        store.ingest(nodes, edges)
        con.close()
        print(f"Successfully synced {len(nodes)} AST nodes and {len(edges)} relation edges.")
        sys.exit(0)

    elif args.command == "audit" and args.subcommand == "verify":
        audit_file = chronos_dir / "chronos-audit.log"
        is_valid, msg, count = verify_audit_chain(audit_file)
        print(f"Audit Chain Verification: {'PASSED' if is_valid else 'FAILED'}")
        print(f"Details: {msg} (Records: {count})")
        sys.exit(0 if is_valid else 1)

if __name__ == "__main__":
    main()
