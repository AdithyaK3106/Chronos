"""Chronos command line interface."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

# Heavy modules (dashboard server, indexer, graph) are imported inside main(), after the
# `chronos hook ...` fast path: hooks run as a new process on every agent tool call.


def _optional_dist_version(dist: str) -> str | None:
    """Installed version of an optional distribution, or None (without importing it)."""
    from importlib import metadata

    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def bench_main():
    """Entry point for `chronos-bench`: explain the missing extra instead of a traceback."""
    if not _optional_dist_version("openhands-sdk"):
        print(
            "`chronos-bench` drives OpenHands agents, which are an optional extra.\n"
            "Install it with:  pip install 'chronos-agent[bench]'",
            file=sys.stderr,
        )
        sys.exit(2)
    from benchmarks.cli import main as _bench

    sys.exit(_bench())


def _hook_fast_path(argv: list[str]) -> None:
    """`chronos hook claude <pre|prompt>`: skip argparse setup and unrelated imports."""
    if len(argv) == 3 and argv[0] == "hook" and argv[1] == "claude" and argv[2] in ("pre", "prompt", "post"):
        from chronos.integrations.claude_code import run_hook

        code, out, err = run_hook(argv[2], sys.stdin.read())
        if out:
            sys.stdout.write(out + "\n")
        if err:
            sys.stderr.write(err)
        sys.exit(code)


def _run_with_claude(args, repo_root: Path) -> int:
    """`chronos run --engine claude`: Claude Code works, the tests decide, failures are fed back."""
    from chronos.orchestrator import ClaudeEngine, run_task
    from chronos.orchestrator.engines import EngineUnavailable
    from chronos.sandbox.runner import SandboxUnavailable

    if not args.test_command:
        print("--engine claude needs --test '<command>': the tests decide whether the task is done.", file=sys.stderr)
        return 2
    model = args.model if args.model and args.model.startswith(("claude", "haiku", "sonnet", "opus")) else None
    engine = ClaudeEngine(model=model, allow_bash=args.allow_bash, timeout_s=args.timeout)
    try:
        out = run_task(
            repo_root, args.prompt, engine, args.test_command, max_rounds=args.max_rounds,
            sandbox=not args.no_sandbox, agent_id=args.agent_id if args.agent_id != "chronos_dev_agent" else "claude_code",
        )
    except (EngineUnavailable, SandboxUnavailable) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        if isinstance(exc, SandboxUnavailable):
            print("Use --no-sandbox to run without it (Chronos hooks still apply if installed).", file=sys.stderr)
        return 2
    for r in out.rounds:
        status = "tests pass" if r.tests_passed else f"tests fail (exit {r.test_exit})"
        extra = f" · engine error: {r.engine_error}" if r.engine_error else ""
        print(f"round {r.round}: {status}{extra}")
    cost = f" · ${out.cost_usd:.4f}" if out.cost_usd else ""
    print(f"{'Done' if out.success else 'Not done'} in {len(out.rounds)} round(s){cost} · run {out.run_id}"
          f"{' · sandboxed' if out.sandboxed else ''}")
    if not out.success and out.final_test_output:
        print("\nLast test output:\n" + out.final_test_output[-1500:])
    return 0 if out.success else 1


def main():
    _hook_fast_path(sys.argv[1:])

    from chronos.audit.logger import verify_audit_chain
    from chronos.cli.ui import add_ui_subparser, launch_ui
    from chronos.context.graph import SQLiteGraphStore
    from chronos.context.indexer import index_repository
    from chronos.core.config import ChronosConfig
    from chronos.core.paths import audit_log_path
    from chronos.core.db import get_connection

    parser = argparse.ArgumentParser(description="Chronos: Bi-temporal intelligence and AI governance")
    subparsers = parser.add_subparsers(dest="command")

    # ui
    add_ui_subparser(subparsers)

    # init
    p_init = subparsers.add_parser("init", help="Initialize .chronos directory in current repository")
    p_init.add_argument("--repo", default=".", help="Repository root path")

    # doctor
    p_doc = subparsers.add_parser("doctor", help="Check database, toolchain, and SLM engine health")
    p_doc.add_argument("--repo", default=".", help="Repository root path")
    p_doc.add_argument("--live", action="store_true", help="Also run a canary event through the real Claude Code hook command")

    # sync
    p_sync = subparsers.add_parser("sync", help="Index repository AST into bi-temporal graph")
    p_sync.add_argument("--repo", default=".", help="Repository root path")

    # config
    p_cfg = subparsers.add_parser("config", help="Inspect or modify Chronos repository configuration")
    p_cfg.add_argument("action", choices=["get", "set", "show"], help="Action (get, set, show)")
    p_cfg.add_argument("key", nargs="?", default=None, help="Configuration key (e.g. slm.enabled, slm.backend)")
    p_cfg.add_argument("value", nargs="?", default=None, help="Configuration value")
    p_cfg.add_argument("--repo", default=".", help="Repository root path")

    # run
    default_model = os.environ.get("CHRONOS_MODEL", os.environ.get("LLM_MODEL"))
    if not default_model:
        import shutil
        default_model = "gemini-3.8-flash-low" if shutil.which("agy") else "gpt-4o"

    p_run = subparsers.add_parser("run", help="Execute an autonomous task with Chronos governance & OpenHands agent")
    p_run.add_argument("prompt", help="Natural-language task prompt")
    p_run.add_argument("--repo", default=".", help="Repository root path")
    p_run.add_argument("--model", default=default_model, help="LLM model (e.g. gpt-4o, claude-3-7-sonnet, gemini-3.8-flash-low)")
    p_run.add_argument("--api-key", default=None, help="LLM provider API key")
    p_run.add_argument("--base-url", default=None, help="Custom LLM API base URL")
    p_run.add_argument("--agent-id", default="chronos_dev_agent", help="Agent identity ID")
    p_run.add_argument("--max-iterations", type=int, default=20, help="Maximum agent turns")
    p_run.add_argument("--timeout", type=int, default=300, help="Task timeout in seconds")
    p_run.add_argument(
        "--allow-terminal",
        action="store_true",
        help=(
            "Give the agent a terminal tool so it can run tests/builds. WARNING: shell commands are "
            "not path-checked by the Chronos firewall (only the tool allowlist applies)."
        ),
    )
    p_run.add_argument(
        "--engine", choices=["openhands", "claude"], default="openhands",
        help="Agent that does the work: the built-in OpenHands agent (default) or Claude Code in headless mode",
    )
    p_run.add_argument("--test", dest="test_command", default=None, help="(--engine claude) Command that decides success, e.g. 'pytest -q'")
    p_run.add_argument("--max-rounds", type=int, default=3, help="(--engine claude) Resume with test failures at most this many rounds")
    p_run.add_argument("--no-sandbox", action="store_true", help="(--engine claude) Run without the `chronos exec` sandbox")
    p_run.add_argument("--allow-bash", action="store_true", help="(--engine claude) Let Claude Code run shell commands")
    p_run.add_argument("--enable-slm", dest="enable_slm", action="store_true", default=None, help="Opt-in to CodeScout-1.7B SLM localization")
    p_run.add_argument("--no-slm", dest="enable_slm", action="store_false", help="Disable SLM localization")
    p_run.add_argument("--slm-backend", choices=["openai_compatible", "ollama", "agy"], default=None, help="SLM serving backend")
    p_run.add_argument("--slm-endpoint", default=None, help="Base URL for SLM server")
    p_run.add_argument("--slm-model", default=None, help="Model identifier")

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
    p_res = subparsers.add_parser("resolve", help="Zero-Path context resolver for natural-language prompts")
    p_res.add_argument("prompt", help="Natural-language task prompt")
    p_res.add_argument("--repo", default=".", help="Repository root path")
    p_res.add_argument("--token-budget", type=int, default=1500, help="Max context token budget")
    p_res.add_argument("--enable-slm", dest="enable_slm", action="store_true", default=None, help="Opt-in to CodeScout-1.7B SLM localization")
    p_res.add_argument("--no-slm", dest="enable_slm", action="store_false", help="Disable SLM localization (pure AST + BM25)")
    p_res.add_argument("--slm-backend", choices=["openai_compatible", "ollama", "agy"], default=None, help="SLM serving backend")
    p_res.add_argument("--slm-endpoint", default=None, help="Base URL for SLM server")
    p_res.add_argument("--slm-model", default=None, help="Model identifier")

    # hook (internal; invoked by Claude Code hooks, reads the event JSON from stdin)
    p_hook = subparsers.add_parser("hook", help="Internal: host-agent hook entry point")
    p_hook.add_argument("host", choices=["claude"])
    p_hook.add_argument("event", choices=["pre", "prompt", "post"])

    # claude (installer)
    p_cl = subparsers.add_parser("claude", help="Install/inspect the Claude Code integration (hooks + MCP)")
    p_cl.add_argument("action", choices=["install", "uninstall", "status"])
    p_cl.add_argument("--scope", choices=["user", "project"], default="user")
    p_cl.add_argument("--repo", default=".", help="Repository (used for --scope project)")
    p_cl.add_argument("--no-mcp", action="store_true", help="Install hooks only")
    p_cl.add_argument("--managed", action="store_true",
                      help="install: write an organisation-wide managed-settings.json (hooks, onFailure: block, allowManagedHooksOnly) to --out")
    p_cl.add_argument("--out", default=None, help="--managed: file to write (default: ./managed-settings.json)")

    # mcp-config (used by install.sh: merge, never overwrite, an editor's MCP config)
    p_mc = subparsers.add_parser("mcp-config", help="Add/remove the Chronos MCP server in an editor's JSON config (keeps other servers)")
    p_mc.add_argument("action", choices=["add", "remove"])
    p_mc.add_argument("path", help="Config file, e.g. ~/.cursor/mcp.json")
    p_mc.add_argument("--command", dest="server_command", default=None, help="Server command (default: this chronos)")

    # exec (OS sandbox around an agent)
    p_exec = subparsers.add_parser(
        "exec",
        help="Run an agent in an OS sandbox: protected files read-only, secrets hidden (Linux)",
        description="Example: chronos exec -- claude",
    )
    p_exec.add_argument("--repo", default=".", help="Repository root path")
    p_exec.add_argument("--agent", default="claude_code", help="Agent identity whose gate approvals apply")
    p_exec.add_argument("--dry-run", action="store_true", help="Show what would be protected and exit")
    p_exec.add_argument("agent_command", nargs=argparse.REMAINDER, help="Command to run, after --")

    # gate (human approval of protected-module edits)
    p_gate = subparsers.add_parser("gate", help="Manage protected-module approvals")
    p_gate.add_argument("action", choices=["grant", "revoke", "list"])
    p_gate.add_argument("pattern", nargs="?", default=None, help="Path pattern, e.g. 'auth.py' or 'src/auth/*'")
    p_gate.add_argument("--agent", default="claude_code", help="Agent identity the approval applies to")
    p_gate.add_argument("--repo", default=".", help="Repository root path")
    p_gate.add_argument("--expires", default=None, help="grant only: expiry, e.g. 2h, 7d or 2026-12-01 (default: never)")

    # approve / revoke: short aliases for `gate grant` / `gate revoke`
    for _name, _help in (("approve", "Allow an agent to edit a protected path (alias of `gate grant`)"),
                         ("revoke", "Remove an approval (alias of `gate revoke`)")):
        _p = subparsers.add_parser(_name, help=_help)
        _p.add_argument("pattern", help="Path or pattern, e.g. 'src/auth/login.py' or 'src/auth/*'")
        _p.add_argument("--agent", default="claude_code", help="Agent identity the approval applies to")
        if _name == "approve":
            _p.add_argument("--expires", default=None, help="Expiry, e.g. 2h, 7d or 2026-12-01 (default: never)")
        _p.add_argument("--repo", default=".", help="Repository root path")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)


    if args.command == "ui":
        code = launch_ui(
            repo_path=getattr(args, "repo", "."),
            host=getattr(args, "host", "127.0.0.1"),
            port=getattr(args, "port", 8765),
            no_browser=getattr(args, "no_browser", False),
            foreground=getattr(args, "foreground", False),
            stop=getattr(args, "stop", False),
        )
        sys.exit(code)

    if args.command == "hook":
        from chronos.integrations.claude_code import run_hook
        code, out, err = run_hook(args.event, sys.stdin.read())
        if out:
            sys.stdout.write(out + "\n")
        if err:
            sys.stderr.write(err)
        sys.exit(code)

    if args.command == "mcp-config":
        from chronos.integrations.mcp_config import ConfigError, add_server, remove_server
        try:
            if args.action == "add":
                from chronos.integrations.claude_code import resolve_cli
                cli = resolve_cli()
                command = args.server_command or cli[0]
                server_args = (cli[1:] if not args.server_command else []) + ["serve"]
                print(add_server(args.path, command, server_args))
            else:
                print(remove_server(args.path))
        except ConfigError as exc:
            print(f"warning: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    if args.command == "claude":
        from chronos.integrations import claude_code as cc
        repo = Path(args.repo).resolve()
        if args.action == "install" and args.managed:
            try:
                for line in cc.install_managed(Path(args.out or "managed-settings.json")):
                    print(line)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                sys.exit(1)
            sys.exit(0)
        if args.action == "install":
            for line in cc.install(args.scope, repo, with_mcp=not args.no_mcp):
                print(line)
            print("Enable a repo with `chronos init` + `chronos sync`; repos without .chronos/ are untouched.")
        elif args.action == "uninstall":
            for line in cc.uninstall(args.scope, repo):
                print(line)
        else:
            print(json.dumps(cc.status(args.scope, repo), indent=2))
        sys.exit(0)

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
        
        # Diagnose only: never create .chronos/ or a database as a side effect.
        if not db_file.is_file():
            print("SQLite WAL DB   : Not created yet (run `chronos init` in this repository)")
        else:
            try:
                con = get_connection(db_file)
                row = con.execute("SELECT count(*) as c FROM sqlite_master WHERE type='table'").fetchone()
                print(f"SQLite WAL DB   : OK ({row['c']} tables)")
                con.close()
            except Exception as e:
                print(f"SQLite WAL DB   : FAILED ({e})")
                status_ok = False

        from chronos.security.gates import protected_config_error
        prot_err = protected_config_error(repo_root)
        if (chronos_dir / "protected.yml").is_file():
            if prot_err:
                print(f"protected.yml   : INVALID, refused ({prot_err}). Until fixed, every edit needs approval.")
                status_ok = False
            else:
                print("protected.yml   : OK")

        try:
            import tree_sitter
            print("Tree-Sitter     : OK")
        except ImportError:
            print("Tree-Sitter     : Missing")
            status_ok = False

        # Optional: only `chronos run` and `chronos-bench` need OpenHands.
        oh_version = _optional_dist_version("openhands-sdk")
        if oh_version:
            print(f"OpenHands SDK   : OK (v{oh_version}, optional)")
        else:
            print("OpenHands SDK   : Not installed (optional; needed only for `chronos run`: pip install 'chronos-agent[agent]')")

        # SLM Diagnostics
        cfg = ChronosConfig.load(repo_root)
        print("\nSLM Localization Engine:")
        print("-----------------------")
        print(f"SLM Active      : {'ENABLED (Opt-In)' if cfg.slm.enabled else 'DISABLED (Default: Pure AST + BM25)'}")
        print(f"Target Model    : {cfg.slm.model}")
        print(f"Backend Engine  : {cfg.slm.backend}")
        print(f"Server Endpoint : {cfg.slm.endpoint}")

        # Endpoint connectivity check (sanitized, non-blocking)
        if cfg.slm.backend in ("openai_compatible", "openai", "vllm", "ollama") and cfg.slm.endpoint:
            probe_url = cfg.slm.endpoint.rstrip("/")
            if cfg.slm.backend == "ollama":
                probe_url = f"{probe_url}/api/tags"
            elif not probe_url.endswith("/models"):
                probe_url = f"{probe_url}/models"
            try:
                t0 = time.time()
                import urllib.request

                req = urllib.request.Request(probe_url, headers={"User-Agent": "Chronos-Doctor/1.0"})
                with urllib.request.urlopen(req, timeout=2) as resp:
                    latency_ms = (time.time() - t0) * 1000
                    print(f"Endpoint Health : ONLINE (HTTP {resp.status}, Ping: {latency_ms:.1f}ms)")
            except Exception:
                print(f"Endpoint Health : OFFLINE / UNREACHABLE (Probe to {probe_url} timed out or failed)")
        elif cfg.slm.backend == "agy":
            import shutil
            has_agy = shutil.which("agy") is not None
            print(f"Endpoint Health : {'READY (agy CLI detected)' if has_agy else 'MISSING (agy CLI not in PATH)'}")

        # Hardware Accelerators
        print("\nHardware Acceleration:")
        print("---------------------")
        try:
            sp = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=2)
            if sp.returncode == 0 and sp.stdout.strip():
                print(f"GPU Detected    : {sp.stdout.strip().replace('\n', ', ')}")
            else:
                print("GPU Detected    : None (CPU Execution)")
        except Exception:
            print("GPU Detected    : None (CPU Execution)")

        npu_detected = False
        for p in ("/sys/class/accel", "/dev/accel0", "/dev/davinciX"):
            if Path(p).exists():
                try:
                    entries = list(Path(p).iterdir()) if Path(p).is_dir() else [p]
                    if entries:
                        npu_detected = True
                        print(f"NPU Detected    : {p} ({len(entries)} devices)")
                        break
                except Exception:
                    pass
        if not npu_detected:
            print("NPU Detected    : None (Unverified / No accelerator found)")

        if args.live:
            from chronos.integrations.claude_code import live_check

            live = live_check()
            print("\nLive Hook Canary (Claude Code):")
            print("------------------------------")
            print(f"Command         : {live['command']}")
            for name, c in live["checks"].items():
                detail = c.get("decision") or c.get("error") or "allow"
                print(f"{name:<16}: {'OK' if c.get('ok') else 'FAILED'} ({detail}, {c.get('ms', '?')} ms)")
                if c.get("stderr"):
                    print(f"                  {c['stderr']}")
            status_ok = status_ok and live["ok"]

        sys.exit(0 if status_ok else 1)

    elif args.command == "config":
        chronos_dir.mkdir(parents=True, exist_ok=True)
        config_file = chronos_dir / "config.json"
        data: dict[str, Any] = {}
        if config_file.is_file():
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        if args.action == "show":
            print(json.dumps(data, indent=2))
        elif args.action == "get":
            if not args.key:
                print(json.dumps(data, indent=2))
            else:
                keys = args.key.split(".")
                curr: Any = data
                for k in keys:
                    if isinstance(curr, dict) and k in curr:
                        curr = curr[k]
                    else:
                        curr = None
                        break
                print(curr)
        elif args.action == "set":
            if not args.key or args.value is None:
                print("Usage: chronos config set <key> <value>", file=sys.stderr)
                sys.exit(1)

            keys = args.key.split(".")
            val: Any = args.value
            if val.lower() in ("true", "1", "yes", "on"):
                val = True
            elif val.lower() in ("false", "0", "no", "off"):
                val = False
            else:
                try:
                    val = int(val)
                except ValueError:
                    pass

            curr = data
            for k in keys[:-1]:
                if k not in curr or not isinstance(curr[k], dict):
                    curr[k] = {}
                curr = curr[k]
            curr[keys[-1]] = val

            with open(config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            print(f"Updated {args.key} = {val} in {config_file}")
        sys.exit(0)

    elif args.command == "sync":
        con = get_connection(db_file)
        store = SQLiteGraphStore(con)
        print(f"Indexing repository {repo_root}...")
        nodes, edges = index_repository(repo_root)
        sync_stats = store.sync_repository(nodes, edges)
        con.close()
        print(f"Successfully synced {len(nodes)} AST nodes and {len(edges)} relation edges.")
        print(
            f"  +{sync_stats['edges_added']} new edges, {sync_stats['edges_invalidated']} invalidated, "
            f"{sync_stats['nodes_removed']} stale nodes removed, {sync_stats['duplicates_removed']} duplicate edges purged."
        )
        sys.exit(0)

    elif args.command == "resolve":
        from chronos.context.session import ChronosSessionBootstrap
        cfg = ChronosConfig.load(repo_root)

        # Apply CLI flag overrides to SLM configuration
        if args.enable_slm is not None:
            cfg.slm.enabled = args.enable_slm
        if getattr(args, "slm_backend", None):
            cfg.slm.backend = args.slm_backend
        if getattr(args, "slm_endpoint", None):
            cfg.slm.endpoint = args.slm_endpoint
        if getattr(args, "slm_model", None):
            cfg.slm.model = args.slm_model

        con = get_connection(db_file)
        boot = ChronosSessionBootstrap(
            repo_path=repo_root,
            con=con,
            enable_slm=cfg.slm.enabled,
            slm_config=cfg.slm,
        )
        res = boot.bootstrap_session(task_prompt=args.prompt, token_budget=args.token_budget)
        con.close()

        mode_str = f"CodeScout-1.7B ({cfg.slm.backend})" if cfg.slm.enabled else "Deterministic AST + BM25"
        print(f"\n🔍 Chronos Zero-Path Semantic Resolution [{mode_str}]:")
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
        # mcp_server loads its config at import time, so pin the repo BEFORE importing it.
        # When launched by an IDE/agent without --repo, find the enclosing repo from the cwd.
        from chronos.core.repo import discover_repo_root
        serve_root = discover_repo_root(repo_root)
        os.environ["CHRONOS_REPO_PATH"] = str(serve_root)
        from chronos.server.mcp_server import main as run_mcp
        sys.stderr.write(f"Starting Chronos FastMCP Server for {serve_root}...\n")
        run_mcp()

    elif args.command == "run" and args.engine == "claude":
        sys.exit(_run_with_claude(args, repo_root))

    elif args.command == "run":
        if not _optional_dist_version("openhands-sdk"):
            print(
                "`chronos run` needs the OpenHands agent runtime, which is an optional extra.\n"
                "Install it with:  pip install 'chronos-agent[agent]'   (or: uv tool install 'chronos-agent[agent]')",
                file=sys.stderr,
            )
            sys.exit(2)
        chronos_dir.mkdir(parents=True, exist_ok=True)
        con = get_connection(db_file)

        # 1. Sync / index repository AST
        print(f"📦 Indexing repository: {repo_root}...")
        store = SQLiteGraphStore(con)
        nodes, edges = index_repository(repo_root)
        store.sync_repository(nodes, edges)

        # 2. Configure developer permissions
        from chronos.security.permissions import set_permissions
        allowed_tools = ["file_editor", "finish", "think"]
        if args.allow_terminal:
            allowed_tools.append("terminal")
        set_permissions(
            con,
            agent_id=args.agent_id,
            manifest={
                "allowed_paths": ["**"],
                "denied_paths": [".git/**", ".env*", "*secret*"],
                "read_only": False,
                "allowed_tools": allowed_tools,
            },
        )

        # 3. Resolve Zero-Path Context
        from chronos.agent.openhands_adapter import ChronosAgentRuntime
        from chronos.agent.agy_llm import AgyLLM
        from openhands.sdk import Tool, Conversation
        from openhands.tools.file_editor import FileEditorTool

        cfg = ChronosConfig.load(repo_root)
        if getattr(args, "enable_slm", None) is not None:
            cfg.slm.enabled = args.enable_slm
        if getattr(args, "slm_backend", None):
            cfg.slm.backend = args.slm_backend
        if getattr(args, "slm_endpoint", None):
            cfg.slm.endpoint = args.slm_endpoint
        if getattr(args, "slm_model", None):
            cfg.slm.model = args.slm_model
        cfg.repo_path = str(repo_root)
        cfg.db_path = str(db_file)
        cfg.audit_log_path = str(audit_log_path(repo_root))

        runtime = ChronosAgentRuntime(config=cfg, db_con=con, graph_store=store)

        mode_desc = f"CodeScout-1.7B ({cfg.slm.backend})" if cfg.slm.enabled else "Deterministic AST + BM25"
        print(f"\n⏳ Resolving Zero-Path Context Envelope [{mode_desc}]...")
        bootstrap_res = runtime.bootstrap.bootstrap_session(
            task_prompt=args.prompt,
            agent_id=args.agent_id,
            refresh_index=False,  # graph was synced in step 1
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
            from chronos.agent.agy_llm import AgyLLM
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

        agent_tools = [Tool(name=FileEditorTool.name)]
        if args.allow_terminal:
            from openhands.tools.terminal import TerminalTool
            agent_tools.append(Tool(name=TerminalTool.name))
            print("⚠️  Terminal tool enabled: shell commands bypass Chronos path checks.\n")

        agent = runtime.create_agent(
            llm=llm,
            agent_id=args.agent_id,
            tools=agent_tools,
        )

        # 5. Execute Conversation with iteration cap & watchdog
        from chronos.agent.openhands_adapter import new_task_id
        task_id = new_task_id()
        conversation_events: list[Any] = []
        convo = Conversation(
            agent=agent,
            workspace=str(repo_root),
            callbacks=[conversation_events.append],
        )
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

        task_outcome = "success"
        task_error = None
        try:
            convo.run()
            status = getattr(getattr(convo, "state", None), "execution_status", None)
            if status is not None:
                status_str = str(getattr(status, "value", status)).lower()
                if "error" in status_str or "stuck" in status_str:
                    task_outcome = "failure"
        except Exception as exc:
            task_outcome = "failure"
            task_error = str(exc)
            raise
        finally:
            done_event.set()
            if timed_out and task_outcome == "success":
                task_outcome = "failure"
                task_error = task_error or f"Timed out after {args.timeout}s"
            # 6. Post-task memory distillation (idempotent per task_id, never raises)
            saved = runtime.record_task_outcome(
                task_id=task_id,
                task_prompt=args.prompt,
                agent_id=args.agent_id,
                outcome=task_outcome,
                files_modified=list(resolved_files),
                events=conversation_events,
                error_message=task_error,
            )
            if saved:
                print(f"\n🧠 Recorded {len(saved)} memories for {task_id}.")
            con.close()

        print("\n🏁 Task execution complete.")
        if timed_out:
            print(f"⚠️ Task reached timeout threshold ({args.timeout}s).")
        sys.exit(0)

    elif args.command == "exec":
        from chronos.core.repo import discover_repo_root
        from chronos.sandbox import SandboxUnavailable, plan_sandbox, run
        from chronos.sandbox.runner import describe

        command = list(args.agent_command)
        if command and command[0] == "--":
            command = command[1:]
        root = discover_repo_root(repo_root)
        db = root / ".chronos" / "chronos.db"
        con = get_connection(db) if db.is_file() else None  # read approvals; never create state
        try:
            plan = plan_sandbox(root, agent_id=args.agent, con=con)
        finally:
            if con is not None:
                con.close()
        if args.dry_run or not command:
            print(describe(plan))
            if not command and not args.dry_run:
                print("\nNo command given. Usage: chronos exec [--repo PATH] -- claude", file=sys.stderr)
                sys.exit(2)
            sys.exit(0)
        s = plan.summary()
        sys.stderr.write(
            f"[chronos] sandbox: {s['read_only']} read-only, {s['hidden']} hidden, "
            f"{s['approved']} approved (writable) in {root}\n"
        )
        try:
            sys.exit(run(plan, command))
        except SandboxUnavailable as exc:
            print(f"[chronos] {exc}\nNot starting the agent without the sandbox.", file=sys.stderr)
            sys.exit(3)

    elif args.command in ("approve", "revoke"):
        from chronos.security.gates import change_gate_approval
        con = get_connection(db_file)
        try:
            print(change_gate_approval(con, repo_root, args.command, args.pattern, args.agent, getattr(args, "expires", None)))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            sys.exit(2)
        finally:
            con.close()
        sys.exit(0)

    elif args.command == "gate":
        from chronos.security.gates import change_gate_approval
        con = get_connection(db_file)
        if args.action == "list":
            rows = con.execute("SELECT agent_id, path_pattern, approved_by, created_at, expires_at FROM gate_approvals ORDER BY created_at").fetchall()
            for r in rows:
                print(f"{r['agent_id']}  {r['path_pattern']}  (by {r['approved_by']}, {r['created_at']}, expires {r['expires_at'] or 'never'})")
            print(f"{len(rows)} approval(s)")
        elif not args.pattern:
            print("Usage: chronos gate grant|revoke <pattern> [--agent ID]", file=sys.stderr)
            sys.exit(1)
        else:
            try:
                print(change_gate_approval(con, repo_root, args.action, args.pattern, args.agent, args.expires))
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                con.close()
                sys.exit(2)
        con.close()
        sys.exit(0)

    elif args.command == "audit" and args.subcommand == "verify":
        audit_file = audit_log_path(repo_root)
        print(f"Audit log: {audit_file}")
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
