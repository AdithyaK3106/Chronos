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

## Body identifiers and stemming (schema v3)

Python functions now index the identifiers and short strings their bodies use (`terms`, split on
snake_case/camelCase), the code FTS table uses the porter stemmer, and long query words also match
by prefix (`expiry` -> `expir*` finds `expires`). Migration v3 rebuilds the code FTS table and removes
the hook's `last_index` marker so the next prompt re-indexes (0.37 s on this checkout's 616-node DB,
checked on a copy).

| Measure (of 20), no paths | resolver fixes | + body terms + stemming |
|---|---|---|
| gold file ranked 1st | 14 | 15 |
| gold file in top 3 | 15 | **17** |
| all gold files in top 5 | 14 | 15 |
| gold file in injected context | 16 | **17** |
| gold function in injected context | 15 | **16** |

"As written" is unchanged (19/20 file ranked 1st, 19/20 gold function in context).

## What is still weak
- **Purely descriptive prompts.** `cng_task_02` without paths ("Fix timezone-naive datetime
  comparison causing premature cylinder expiry flag") still finds nothing useful: keyword search
  cannot connect "premature expiry flag" to `expires_at < now`. Below the injection threshold the
  hook injects nothing, so the agent is not misled, but it gets no help either. Closing this needs
  meaning-based search (a small local embedding model; code stays on the machine). Open decision.
- The extra ~20 ms per prompt is the Python `ast` parse for abstract detection (cached by mtime).

## Implication for the agent benchmark
The 4 Nov agent benchmark could not show a localization benefit: every prompt named its file and
the harness also passed `target_files` to the Chronos arm. The agent's tools were also limited to
`file_editor`/`finish`/`think` (no terminal), so neither arm could run tests. A fair efficiency
benchmark needs "no paths" prompts, no `target_files` for the treatment arm, and a terminal tool.
