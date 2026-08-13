"""Deprecated per-wedge entry points.

Before unification Chronos shipped four MCP servers. Existing agent configs
point at those command names, so they keep working -- each now starts the single
unified server and prints a deprecation notice to stderr (never stdout, which
carries the MCP protocol).

Rationale: breaking a partner's config to tidy our own naming is a bad trade.
These are three lines each and cost nothing to keep for a release cycle.
"""

import sys

from .server import main as _main

_NOTE = ("[chronos] {old} is deprecated. Use chronos-mcp instead — it serves "
         "every tool from all four wedges. This alias will be removed in a "
         "future release.")


def _run(old):
    print(_NOTE.format(old=old), file=sys.stderr)
    _main()


def graph():
    _run("chronos-graph-mcp")


def ledger():
    _run("chronos-ledger-mcp")


def playbook():
    _run("chronos-playbook-mcp")


def enforce():
    _run("chronos-enforce-mcp")
