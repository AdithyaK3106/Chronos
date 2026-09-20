# Push/Pull MCP Stress Test

All operations executed STRICTLY via `mcp.ClientSession` standard I/O.

- **chronos_capture_lesson (x20)**: OK - `Inserted 20 lessons concurrently.`
- **chronos_query_playbook (x50)**: OK - `Executed 50 concurrent playbook queries. Errors/Timeouts: 0`
- **chronos_enforce (x30 massive diffs)**: OK - `Parsed 30 x 10,000-line diffs concurrently. Errors: 0. AST parsing held up.`
