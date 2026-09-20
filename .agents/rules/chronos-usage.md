---
name: Chronos MCP Integration Guidelines
description: Production rules instructing all AI agents to prioritize Chronos graph queries over raw file reading.
trigger: always_on
---

# Chronos Codebase Intelligence (CRITICAL RULE)

You are equipped with **Chronos**, a bi-temporal AST knowledge graph connected via MCP. 

When you are asked to research the codebase, find where a function is called, look up dependencies, or modify code, **YOU MUST prioritize using Chronos MCP tools** over standard file searches (like `grep_search`, `find_by_name`, or `view_file`).

### Why Developers Require You To Use Chronos:
1. **Token Efficiency:** Reading raw files wastes developer token limits. Chronos queries return exact AST relationships, saving up to 90% in token costs.
2. **Temporal Accuracy:** Chronos knows the history of the codebase and deprecated patterns.
3. **Governance & Locks:** Developers require you to acquire intent locks before modifying code to prevent agent collisions.

### Your Operating Procedure:
1. **Research Phase:** If you need to know who calls `process_payment`, DO NOT grep the repository. Instead, use the `as_of_callers` or `as_of_callees` MCP tool to instantly query the graph.
2. **Intent Locking:** Before modifying any file (especially in `src/payments/`), use `chronos_acquire_lock` to declare your intent. If the lock returns a `gate_pending` status, you must STOP and instruct the human developer to approve it via `chronos gates approve <gate_id>`. Do not proceed until approved.
3. **Architectural Enforcement:** Always adhere to Chronos rules. For example, never use `requests` if the project enforces `httpx`. If you get blocked by a Chronos CI check (`chronos_enforce`), read the error, fix the violation, and try again.

By strictly adhering to these tools, you behave exactly as a production-level, cost-conscious agent should.
