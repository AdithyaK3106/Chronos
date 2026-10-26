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

    # run
    default_model = os.environ.get("CHRONOS_MODEL", os.environ.get("LLM_MODEL"))
    if not default_model:
        import shutil
        default_model = "gemini-3.8-flash-low" if shutil.which("agy") else "gpt-4o"

    p_run = subparsers.add_parser("run", help="Execute an autonomous task with Chronos governance & OpenHands agent")
    p_run.add_argument("prompt", help="Natural-language task prompt")
    p_run.add_argument("--repo", default=".", help="Repository root path")
    p_run.add_argument("--model", default=default_model, help="LLM model (e.g. gpt-4o, claude-3-7-sonnet, ollama/qwen2.5-coder, gemini-3.8-flash-low)")
    p_run.add_argument("--api-key", default=None, help="LLM provider API key (or set OPENAI_API_KEY / ANTHROPIC_API_KEY / LLM_API_KEY)")
    p_run.add_argument("--base-url", default=None, help="Custom LLM API base URL (e.g. for Ollama / vLLM)")
    p_run.add_argument("--agent-id", default="chronos_dev_agent", help="Agent identity ID")
    p_run.add_argument("--max-iterations", type=int, default=20, help="Maximum agent turns")
    p_run.add_argument("--timeout", type=int, default=300, help="Task timeout in seconds")

    # serve
    p_serve = subparsers.add_parser("serve", help="Start FastMCP server for IDE / external agent integration")
    p_serve.add_argument("--repo", default=".", help="Repository root path")

    # audit verify
    p_audit = subparsers.add_parser("audit", help="Audit log commands")
    p_audit.add_argument("subcommand", choices=["verify"], help="Subcommand (verify)")
    p_audit.add_argument("--repo", default=".", help="Repository root path")

    # memory
    p_mem = subparsers.add_parser("memory", help="Epistemic living memory operations")
    p_mem_sub = p_mem.add_subparsers(dest="action")

    p_mem_rem = p_mem_sub.add_parser("remember", help="Store a memory")
    p_mem_rem.add_argument("content", help="Memory content / invariant / lesson")
    p_mem_rem.add_argument("--tier", choices=["core", "learned", "episodic", "working", "procedural"], default="learned")
    p_mem_rem.add_argument("--importance", type=float, default=0.5)
    p_mem_rem.add_argument("--tags", default="", help="Comma-separated tags")
    p_mem_rem.add_argument("--namespace", default=None, help="Repository namespace (defaults to repo name)")
    p_mem_rem.add_argument("--repo", default=".", help="Repository root path")

    p_mem_rec = p_mem_sub.add_parser("recall", help="Search and rank relevant memories")
    p_mem_rec.add_argument("query", help="Search query")
    p_mem_rec.add_argument("--limit", type=int, default=5, help="Maximum memories to return")
    p_mem_rec.add_argument("--tier", default=None, help="Filter by tier")
    p_mem_rec.add_argument("--namespace", default=None, help="Repository namespace")
    p_mem_rec.add_argument("--repo", default=".", help="Repository root path")

    p_mem_lst = p_mem_sub.add_parser("list", help="List stored memories")
    p_mem_lst.add_argument("--tier", default=None, help="Filter by tier")
    p_mem_lst.add_argument("--outcome", default=None, help="Filter by outcome")
    p_mem_lst.add_argument("--limit", type=int, default=20, help="Maximum memories to list")
    p_mem_lst.add_argument("--namespace", default=None, help="Repository namespace")
    p_mem_lst.add_argument("--repo", default=".", help="Repository root path")

    p_mem_stat = p_mem_sub.add_parser("stats", help="Show memory store statistics")
    p_mem_stat.add_argument("--namespace", default=None, help="Repository namespace")
    p_mem_stat.add_argument("--repo", default=".", help="Repository root path")

    p_mem_proc = p_mem_sub.add_parser("procedures", help="List active procedural behavioral rules")
    p_mem_proc.add_argument("--namespace", default=None, help="Repository namespace")
    p_mem_proc.add_argument("--repo", default=".", help="Repository root path")

    # resolve
    p_res = subparsers.add_parser("resolve", help="Zero-Path SLM context resolver for natural-language prompts")
    p_res.add_argument("prompt", help="Natural-language task prompt")
    p_res.add_argument("--repo", default=".", help="Repository root path")
    p_res.add_argument("--token-budget", type=int, default=1500, help="Max context token budget")

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

    elif args.command == "resolve":
        from runtime.session import ChronosSessionBootstrap
        con = get_connection(db_file)
        boot = ChronosSessionBootstrap(repo_path=repo_root, con=con)
        res = boot.bootstrap_session(task_prompt=args.prompt, token_budget=args.token_budget)
        con.close()

        print(f"\n🔍 Chronos Zero-Path Semantic Resolution:")
        resolved_files = res.get("resolved_files", [])
        resolved_symbols = res.get("resolved_symbols", [])
        test_files = res.get("test_files", [])
        print(f"  Target Files    : {', '.join(resolved_files) if resolved_files else 'None'}")
        print(f"  Target Symbols  : {', '.join(resolved_symbols) if resolved_symbols else 'None'}")
        print(f"  Associated Tests: {', '.join(test_files) if test_files else 'None'}")
        print(f"  Memories Injected: {res.get('memories_count', 0)}")
        print(f"\n{res.get('provenance_summary', '')}\n")
        sys.exit(0)

    elif args.command == "serve":
        from chronos.server.mcp_server import main as run_mcp
        sys.stderr.write(f"Starting Chronos FastMCP Server for {repo_root}...\n")
        run_mcp()

    elif args.command == "run":
        chronos_dir.mkdir(parents=True, exist_ok=True)
        con = get_connection(db_file)

        # 1. Sync / index repository AST
        print(f"📦 Indexing repository: {repo_root}...")
        store = SQLiteGraphStore(con)
        nodes, edges = index_repository(repo_root)
        store.ingest(nodes, edges)

        # 2. Configure developer permissions
        from chronos.security.permissions import set_permissions
        set_permissions(
            con,
            agent_id=args.agent_id,
            manifest={
                "allowed_paths": ["**"],
                "denied_paths": [".git/**", ".env*", "*secret*"],
                "read_only": False,
                "allowed_tools": ["file_editor", "finish", "think"],
            },
        )

        # 3. Resolve Zero-Path Context
        from runtime.openhands_adapter import ChronosAgentRuntime
        from runtime.agy_llm import AgyLLM
        from openhands.sdk import Tool, Conversation
        from openhands.tools.file_editor import FileEditorTool

        cfg = ChronosConfig(
            repo_path=str(repo_root),
            db_path=str(db_file),
            audit_log_path=str(chronos_dir / "chronos-audit.log"),
        )
        runtime = ChronosAgentRuntime(config=cfg, db_con=con, graph_store=store)

        print("\n⏳ Resolving Zero-Path Context Envelope...")
        bootstrap_res = runtime.bootstrap.bootstrap_session(
            task_prompt=args.prompt,
            agent_id=args.agent_id,
        )
        resolved_files = bootstrap_res.get("resolved_files", [])
        resolved_symbols = bootstrap_res.get("resolved_symbols", [])
        test_files = bootstrap_res.get("test_files", [])

        if resolved_files:
            print(f"  Target Files   : {', '.join(resolved_files[:3])}")
        if resolved_symbols:
            print(f"  Target Symbols : {', '.join(resolved_symbols[:5])}")
        if test_files:
            print(f"  Related Tests  : {', '.join(test_files[:3])}")
        print(f"  Injected Context: {bootstrap_res.get('token_count', 0)} tokens")

        # 4. Instantiate Agent-Agnostic LLM Provider
        print(f"\n🚀 Booting Governed OpenHands Agent (Model: {args.model})...\n")
        import shutil
        if args.model.startswith("agy:") or (shutil.which("agy") and (args.model.startswith("gemini-3.8") or args.model == "agy")):
            from runtime.agy_llm import AgyLLM
            clean_model = args.model.removeprefix("agy:")
            llm = AgyLLM(model=clean_model)
        else:
            from openhands.sdk import LLM
            llm_kwargs: dict[str, Any] = {"model": args.model}
            resolved_key = (
                args.api_key
                or os.environ.get("LLM_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("GEMINI_API_KEY")
            )
            if resolved_key:
                llm_kwargs["api_key"] = resolved_key
            resolved_base = args.base_url or os.environ.get("LLM_BASE_URL")
            if resolved_base:
                llm_kwargs["base_url"] = resolved_base
            llm = LLM(**llm_kwargs)

        agent = runtime.create_agent(
            llm=llm,
            agent_id=args.agent_id,
            tools=[Tool(name=FileEditorTool.name)],
        )

        # 5. Execute Conversation with iteration cap & watchdog
        convo = Conversation(agent=agent, workspace=str(repo_root))
        convo.max_iteration_per_run = args.max_iterations
        convo.send_message(bootstrap_res["augmented_prompt"])

        import threading
        timed_out = False

        def _watchdog():
            nonlocal timed_out
            if not done_event.wait(args.timeout):
                timed_out = True
                if hasattr(convo, "cancel_token") and convo.cancel_token:
                    convo.cancel_token.cancel()
                elif hasattr(convo, "pause"):
                    convo.pause()

        done_event = threading.Event()
        watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
        watchdog_thread.start()

        try:
            convo.run()
        finally:
            done_event.set()

        con.close()
        print("\n🏁 Task execution complete.")
        if timed_out:
            print(f"⚠️ Task reached timeout threshold ({args.timeout}s).")
        sys.exit(0)

    elif args.command == "audit" and args.subcommand == "verify":
        audit_file = chronos_dir / "chronos-audit.log"
        is_valid, msg, count = verify_audit_chain(audit_file)
        print(f"Audit Chain Verification: {'PASSED' if is_valid else 'FAILED'}")
        print(f"Details: {msg} (Records: {count})")
        sys.exit(0 if is_valid else 1)

    elif args.command == "memory":
        from chronos.memory.store import ChronosMemoryStore
        from chronos.memory.models import MemoryTier, OutcomeType

        con = get_connection(db_file)
        store = ChronosMemoryStore(con=con)
        ns = args.namespace if getattr(args, "namespace", None) else repo_root.name

        if args.action == "remember":
            tags_list = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
            mid = store.remember(
                content=args.content,
                repo_namespace=ns,
                tier=MemoryTier(args.tier),
                importance=args.importance,
                tags=tags_list,
                source="cli",
            )
            print(f"Stored memory #{mid} [tier: {args.tier}, namespace: {ns}]")

        elif args.action == "recall":
            results = store.recall(
                query=args.query,
                repo_namespace=ns,
                tier=args.tier,
                limit=args.limit,
            )
            print(f"Recall results for '{args.query}' in [{ns}] ({len(results)} matches):")
            for r in results:
                print(f"  #{r.id} [{r.tier.value.upper()}] (score: {r.score:.3f}): {r.content}")
                if r.evidence:
                    print(f"     Evidence: {r.evidence[:100]}...")

        elif args.action == "list":
            records = store.list_memories(
                repo_namespace=ns,
                tier=args.tier,
                outcome=args.outcome,
                limit=args.limit,
            )
            print(f"Memories in [{ns}] ({len(records)} found):")
            for r in records:
                print(f"  #{r.id} [{r.tier.value}] [{r.outcome.value}]: {r.content[:100]}")

        elif args.action == "procedures":
            procs = store.get_procedures(repo_namespace=ns)
            print(f"Active procedural rules in [{ns}] ({len(procs)} total):")
            for p in procs:
                print(f"  - {p}")

        elif args.action == "stats":
            stats = store.stats(repo_namespace=ns)
            print("Chronos Memory Statistics:")
            print(f"  Total Memories  : {stats.total_memories}")
            print(f"  Archived        : {stats.archived_count}")
            print(f"  By Tier         : {stats.by_tier}")
            print(f"  By Outcome      : {stats.by_outcome}")
            print(f"  Namespaces      : {', '.join(stats.namespaces) or 'None'}")

        else:
            p_mem.print_help()

        con.close()
        sys.exit(0)

if __name__ == "__main__":
    main()
