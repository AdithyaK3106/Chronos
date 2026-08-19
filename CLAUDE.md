# Chronos

## Kuzu: one process holds the graph, ever

Kuzu (the embedded graph DB backing `chronos/store.py`) allows exactly **one
process** to have the store open at a time. `open_driver()` blocks/raises
(`GraphLocked`) if a second process tries.

Consequence: **do not spawn multiple subagents (or any parallel processes)
that each open the graph directly** — via `chronos index`, `chronos sync`,
`chronos doctor`, a standalone `open_driver()` script, or a second
`chronos-mcp` server instance. They will contend for the same lock; the loser
either hangs (pre-fix behavior) or fails fast with `GraphLocked` (current
behavior, see `chronos/store.py::_holder_hint`).

The one safe way to get concurrent access is through a single already-running
MCP server (`chronos/server.py`), which serializes all tool calls onto one
process-wide driver (`wedge1_mcp.driver()`). Multiple agents/clients can call
*that* server's tools concurrently — the contention is handled inside the
process, not across processes.

If you need to verify a change against the live graph while an MCP server is
already running and holding the lock, don't spawn a second server or a
subprocess that opens the graph — either use the already-connected MCP tools,
or build a throwaway graph in a temp dir (`CHRONOS_DB=<tmp>/g.kz`) to avoid
lock contention entirely.
