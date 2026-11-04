# Large-repo check, 4 Nov 2026

Scratch repo built from real code (the `.py` files of litellm, openhands, pydantic, starlette and
google from this venv): **3,895 files, 1.23M lines, 50,724 symbols, 218,963 call edges, 277 MB DB.**
On `/data` (a FUSE mount, slower than a local disk). Hooks run as Claude Code runs them: one
process per call. Claude Code's limits: 30 s for the prompt hook, 10 s for tool hooks.
Script: `/data/chronos-bench-scratch/bigrepo_bench.py`.

| Hook | Before | After |
|---|---|---|
| prompt, first call (full index) | **120 s: killed at 30 s, so it never finished and restarted every prompt** | 1.6 s (indexes in the background: 110 s, then context starts) |
| prompt, nothing changed | 3.5 s | 1.5 s |
| prompt, one file edited (`router.py`, 14.8k lines) | 10 s | 2.75 s |
| `update_file` for that edit | 8.0 s | 0.65 s |
| pre: Edit / protected Edit / Read `.env` | 0.14-0.17 s | 0.12-0.16 s |
| pre / post Bash | 0.45 / 0.30 s | 0.31 / 0.41 s |

## Fixes
- **Background first index.** If a full index would parse more than 400 files
  (`CHRONOS_INLINE_INDEX_MAX_FILES`), the prompt hook starts a detached
  `python -m chronos.integrations.claude_code --index <repo>` (one at a time, `.chronos/indexing.lock`,
  stale after an hour) and injects nothing until it is done. Tool hooks keep working meanwhile.
- **Incremental updates write only the difference** (removed edges invalidated, new edges added,
  changed nodes upserted, one transaction). Before, every edit rewrote all of the file's nodes and
  edges and grew the edge history by the file's full edge count.
- **`code_edges.dst_name`** (indexed bare callee name, schema v3, backfilled by the migration):
  `as_of_callers` used `LIKE '%::name::%'`, a scan of all edges per call (~250 ms each, 5 per prompt).
- **`as_of_impact` fixed**: it joined on node ids although edges store bare names, so it found no
  transitive callers (the chronos_task_01 bug, still present in Chronos itself). It now returns each
  transitive caller once at its shortest depth, never the symbol itself; the task's hidden
  acceptance tests pass (2 of 4 failed before).
- Batched inserts (`executemany`), a range query instead of `src LIKE 'path::%'`, and a deque in the
  body-term walk (list.pop(0) was quadratic).

## Still slow-ish
- The prompt hook's freshness walk (`os.walk` over the repo on every prompt) is ~0.3 s here and
  grows with file count. A file-watcher or git-based change detection would remove it.
- A full index of 50k symbols takes ~2 minutes; fine in the background.
