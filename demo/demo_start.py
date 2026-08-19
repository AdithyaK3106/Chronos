"""make demo-start: confirm the pre-built graph/rules are in place and print
the .mcp.json block to copy into Claude Code / Cursor.

chronos-mcp is a stdio MCP server (chronos/server.py) -- it's meant to be
spawned BY an MCP client via .mcp.json, not run standalone as a long-lived
process a human starts first. So "starting" the demo means: verify the
fixture is ready, then hand over the config that makes an agent spawn it.
"""

import sys
from pathlib import Path

NOVAPAY = Path(__file__).resolve().parent / "novapay"


def main():
    graph = NOVAPAY / ".chronos" / "graph.kz"
    rules = NOVAPAY / ".chronos" / "chronos.db"
    mcp = NOVAPAY / ".mcp.json"

    missing = [p for p in (graph, rules, mcp) if not p.exists()]
    if missing:
        print("missing demo fixtures, run `make demo` (or python demo/build_graph.py "
              "&& python demo/seed_rules.py) first:")
        for p in missing:
            print(f"  - {p}")
        sys.exit(1)

    print(f"graph ready:  {graph}")
    print(f"rules ready:  {rules}")
    print()
    print(f"{mcp} is already configured. To connect your own Claude Code / Cursor session:")
    print(f"  cd {NOVAPAY}")
    print("  claude   # or: open Cursor here -- both pick up .mcp.json automatically")
    print()
    print(mcp.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
