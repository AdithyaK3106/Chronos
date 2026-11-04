# Retrieval benchmark, 4 Nov 2026

`python -m benchmarks.retrieval` (no model calls, ~90 s for 20 tasks). For each efficacy task, in a
worktree at its commit: index, resolve the prompt exactly as the Claude Code hook does (no
`target_files`, no memories), and score against what the reference fix changes. Gold functions are
the innermost qualified definitions (`Class.method`) around each changed line of the original file.
Full per-task results: `artifacts/retrieval_2026-11-04.json`.

Two prompt variants:
- **as written**: all 20 prompts name their target file, so this mostly tests ranking.
- **no paths**: file paths removed, so retrieval has to find the code itself.

## Before and after the resolver fixes (same scorer)

| Measure (of 20) | as written: before | after | no paths: before | after |
|---|---|---|---|---|
| gold file ranked 1st | 18 | **19** | 12 | **14** |
| gold file in top 3 | 20 | 20 | 16 | 15 |
| all gold files in top 5 | 19 | 19 | 14 | 14 |
| gold file in injected context | 20 | 20 | 16 | 16 |
| gold function in top 3 | 16 | **18** | 12 | **13** |
| gold function in injected context | 17 | **19** | 14 | **15** |
| median resolve time | 15 ms | 40 ms | 17 ms | 28 ms |

## Fixes
1. **Symbols in a file the prompt names get +35.** "Fix as_of_impact() in chronos/context/graph.py"
   used to rank same-named MCP wrappers in `mcp_server.py` first.
2. **Abstract definitions get -20** (`@abstractmethod`, or a body that is only `...`, `pass`,
   `raise NotImplementedError` or a placeholder `return []`). This is the "context points at the
   abstract method" problem from the 4 Nov benchmark (chronos_task_01).
3. **Methods match their bare name.** Methods are stored as `Class.method`, so `as_of_impact()`
   in a prompt never exactly matched `SQLiteGraphStore.as_of_impact`. Opt-in
   (`get_nodes_by_name(..., include_members=True)`), so call-edge linking is unchanged.
4. **Exact name matches sort first** in `get_nodes_by_name`; the context builder takes the first row.
5. **Seed symbols keep score order.** They were sorted alphabetically, so the token budget dropped
   symbols by name instead of by relevance.

## What is still weak
- **Prompts without identifiers.** `cng_task_02` without paths ("Fix timezone-naive datetime
  comparison causing premature cylinder expiry flag") returns no candidates: the lexical index
  holds names, paths, signatures and docstrings, not the identifiers used inside function bodies.
  4 of 5 CNG tasks miss the gold file without paths for this reason. Indexing body identifiers is
  the next retrieval improvement (schema change + migration).
- The extra ~20 ms per prompt is the Python `ast` parse for abstract detection (cached by mtime).

## Implication for the agent benchmark
The 4 Nov agent benchmark could not show a localization benefit: every prompt named its file and
the harness also passed `target_files` to the Chronos arm. The agent's tools were also limited to
`file_editor`/`finish`/`think` (no terminal), so neither arm could run tests. A fair efficiency
benchmark needs "no paths" prompts, no `target_files` for the treatment arm, and a terminal tool.
