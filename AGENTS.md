# Chronos Repository & Agent Rules

All AI coding agents (including Antigravity / `agy`) operating on this codebase must strictly use the Chronos MCP tools (`chronos`) for:
1. AST and temporal analysis (`as_of_callers`, `as_of_callees`, `as_of_impact`, `as_of_diff`, `what_changed`).
2. Sub-file intent locking before code modifications (`chronos_check_conflicts`, `chronos_acquire_lock`).
3. Rule playbook queries and CI gate enforcement (`chronos_query_playbook`, `chronos_enforce`).
4. Provenance tracking and lock release after updates (`chronos_log_provenance`, `chronos_release_lock`).
