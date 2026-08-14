"""Resident Chronos process: keeps the expensive imports and the Kuzu driver warm.

IMPORTANT: nothing in this package may import chronos.cli, chronos.sync or
anything else that pulls in graphiti_core at module scope. That chain costs
~3.9s (graphiti_core -> openai + neo4j), and paying it in the client would
defeat the entire point -- measured before this package was written:

    bare interpreter        70 ms
    json + socket          108 ms
    import chronos.cli    5039 ms   <- what the CLI pays today

The daemon pays that once at startup; clients stay on the cheap side of the
line. Heavy imports live inside functions, never at module top level.
"""
