"""`python -m chronos ...` -> the same CLI as the `chronos` console script.

One argument parser, not two. Defining subcommands here as well would mean two
places to keep in sync and two ways for them to disagree.

The one exception is the daemon fast path below, and it exists because of a
measurement: `import chronos.cli` costs ~5.0s on this machine (graphiti_core
pulls in openai and neo4j), against 70ms for a bare interpreter. A daemon that
only avoided re-opening the Kuzu driver would save nothing -- the client would
still pay the import chain before it could ask. So the daemon check has to
happen BEFORE chronos.cli is imported, which means a small amount of argv
handling here rather than in the parser.

It stays deliberately dumb: recognise the two hot subcommands, hand anything
else (or any flag it does not understand, or any failure at all) straight to
the real parser. It never decides an outcome the full CLI would decide
differently -- it only chooses who computes the answer.
"""

import sys


def _fast_path(argv: list[str]) -> int | None:
    """Serve `enforce`/`index` from the daemon. None = fall through to the CLI.

    Returns an exit code only when the daemon actually answered.
    """
    from .daemon import protocol
    if protocol.daemon_disabled():
        return None

    # Only the flags the daemon knows how to honour. Anything else -- --help,
    # --agent-id, a typo -- goes to argparse, which owns errors and usage.
    known = {"--repo", "--file", "--lang", "--diff", "--group", "--mode",
             "--agent-id", "--session-id"}
    flags = {"--fail-on-block", "--exit-code"}
    cmd, rest = argv[0], argv[1:]
    opts: dict[str, str] = {}
    seen: set[str] = set()
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in flags:
            seen.add(a)
            i += 1
            continue
        if a in known:
            if i + 1 >= len(rest):
                return None            # missing value: let argparse report it
            opts[a] = rest[i + 1]
            i += 2
            continue
        return None                    # unknown token: not ours to interpret
    # The global --repo form (`chronos --repo X enforce`) never reaches here,
    # since argv[0] would not be the subcommand. That path takes the slow route,
    # which is correct but worth knowing.

    from .daemon.client import DaemonClient
    client = DaemonClient()
    if not client.available():
        return None

    if cmd == "enforce":
        report = client.enforce(
            repo=opts.get("--repo") or ".", file=opts.get("--file"),
            lang=opts.get("--lang"), diff=opts.get("--diff"),
            group=opts.get("--group") or "default",
            agent_id=opts.get("--agent-id"), session_id=opts.get("--session-id"),
            exit_code=bool(seen))
        if report is None:
            return None                # daemon gone or errored: fall back
        if report.get("empty"):
            print("no changed files to check")
            return 0
        _print_report(report)
        return 1 if (report.get("blocks") and seen) else 0

    if cmd == "index":
        r = client.index(repo=opts.get("--repo") or ".",
                         group=opts.get("--group") or "default",
                         mode=opts.get("--mode") or "fast")
        if r is None:
            return None
        print(f'indexed {opts.get("--repo") or "."}: {r["nodes"]} nodes, '
              f'{r["edges"]} edges in {r["indexed_s"]}s')
        print(f'synced {r["group"]} @ {r["at"]}: nodes={r["nodes"]} '
              f'added={r["added"]} invalidated={r["invalidated"]} '
              f'unchanged={r["unchanged"]}')
        return 0
    return None


def _print_report(report: dict):
    """Same rendering as cli.print_enforce_report, without importing it.

    Duplicated on purpose: importing chronos.cli here would cost the 5s this
    path exists to avoid. The shape is pinned by a test that asserts both
    renderers produce identical output.
    """
    for row in report.get("rows", []):
        if row["verdict"] == "ok":
            print(f'OK     {row["file"]}')
            continue
        loc = f'{row["file"]}:{row["line"]}' if row.get("line") else row["file"]
        print(f'{row["verdict"].upper():<6} {loc}  rule:{row["rule_id"]}  '
              f'"{row["message"]}"')
    tail = (f' ({report["skipped"]} skipped: no rules for that file type)'
            if report.get("skipped") else "")
    print(f'Checked {report["checked"]} files - {report["blocks"]} block, '
          f'{report["warns"]} warn, {report["oks"]} ok{tail}')


def main():
    argv = sys.argv[1:]
    if argv and argv[0] in ("enforce", "index"):
        try:
            code = _fast_path(argv)
        except Exception:  # noqa: BLE001 -- the fast path is an optimisation,
            code = None    # never a reason for the CLI to fail
        if code is not None:
            sys.exit(code)
    from .cli import main as cli_main
    cli_main()


if __name__ == "__main__":
    main()
