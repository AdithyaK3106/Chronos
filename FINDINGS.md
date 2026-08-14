# Chronos End-to-End Pilot — Findings

**Date:** 2026-08-14
**Repo A:** `C:/Users/urbra/OneDrive/Desktop/Projects/MediAssist` (git, Python)
**Repo B:** `C:/Users/urbra/OneDrive/Desktop/Projects/Cooling project` (git, Python, **path contains a space**)
**Chronos:** `C:/Users/urbra/OneDrive/Desktop/Projects/New ortho` @ `b75176e`

Test plan says "cooling project"; the actual directory is `Cooling project`.
The plan's `find` one-liner missed it (it matches `cooling` exactly, not
`cooling project`), so the path was found by listing the parent.

---

## Phase 0 — Baseline

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 0.0 `doctor --repo <A>` (flag placement as written in plan) | runs | `error: unrecognized arguments: --repo` — `--repo` is a **global** flag (`chronos --repo X doctor`), but `init`/`enforce` define their own subcommand-level `--repo`. Inconsistent surface. | **FAIL** |
| 0.0b exit code on that arg error | non-zero | argparse exits 2, but `$?` read through the pipe showed 0 — my harness artifact, not a Chronos bug. Re-tested below. | UNEXPECTED |
| 0.1 `chronos --repo <A> doctor` before init | warns about missing `.chronos/`, exits non-zero | **Reported the Chronos repo's OWN data** (`601 nodes`), not MediAssist's. Never mentions `.chronos/` missing. Exit 0. | **FAIL** |
| 0.1b `doctor` repo B | B's data | **Byte-identical to repo A's output** — same `-tests.db`, same 601 nodes. Confirms BUG-1. | **FAIL** |
| 0.2 `enforce --exit-code` before index | graceful, no crash | `Checked 0 files - 0 block, 0 warn, 0 ok`, exit 0. No crash. | **PASS** |
| 0.2b enforce on an explicit `.py` file, pre-index | graceful | `OK <file>`, exit 0. | **PASS** |
| 0.3 query before index | empty / "not indexed", no crash | `no_data_reason: "symbol '...' is not present in the graph for any time period"`. Explicit, not silent. | **PASS** |

### BUG-1 (severity: wrong) — `doctor` is repo-blind; `--repo` does not scope it

`do_doctor` resolves the upstream DB via `find_db()`, which is:

```python
# chronos/upstream.py:30
def find_db(cache_dir=None):
    """Newest .db/.sqlite file in the cache dir, or None."""
    dbs = [p for p in d.rglob("*") if p.suffix in (".db",...)]
    return max(dbs, key=lambda p: p.stat().st_mtime) if dbs else None
```

It picks the **newest database in a shared global cache by mtime**, ignoring
`--repo` entirely. Consequences observed:

- `chronos --repo <MediAssist> doctor` printed New ortho's graph (601 nodes).
- It selected `...New-ortho-tests.db` — a stale artifact from an unrelated test
  run — purely because it had the newest mtime.
- The `chronos:` line reads from `~/.chronos/` (a single global store), so node
  counts are global, not per-repo.

Impact: doctor cannot be trusted to describe the repo you name. Anyone
onboarding a second repo gets confident, wrong output. Every doctor reading in
this report is therefore annotated with which DB it actually used.

Correct behavior: `--repo` should derive the cache DB name from the repo path
(the cache filenames already encode it: `C-Users-...-MediAssist.db`), and
`doctor` should say "not indexed" when that specific DB is absent.

### BUG-2 (severity: cosmetic) — no "missing .chronos/" warning pre-init

Plan expected doctor to flag an uninitialized repo. It has no such check; it
reports the global store's health instead. Related to BUG-1 — doctor has no
concept of "this repo".


### Note on 0.2 — "0 files" was correct, but silently so

`enforce` defaults to `--diff HEAD~1`. That commit in repo A touched 26 files,
all `.md`/config → `node_language` = `unknown` → skipped at `cli.py:331`. The
verdict line reads `Checked 0 files` with no indication that 26 files were seen
and skipped. Correct logic, but indistinguishable from "enforce is broken" or
"the diff was empty". Suggest reporting skipped-file count.

### Note — `chronos_query_timeline` does not exist

The plan references it in 0.3, 2.3, 3.1, 3.2, 3.5. The actual Wedge 1 MCP tools
are `as_of_callers`, `as_of_callees`, `as_of_impact`, `what_changed`,
`index_health` (STATUS D-1 records that the names were deliberately kept). There
is also no `chronos_gc` MCP tool (gc is CLI-only). Timeline-ish tests were run
against `query.callers` / `what_changed` instead, in-process (same code path).

---

## Phase 1 — Init

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 1.1 `init --repo <A>` | `.chronos/` + config + hooks + doctor | `.chronos/` with `config.json`, `logs/`, `rules/`. Hooks written. Doctor ran (showing wrong repo — BUG-1). Exit 0. | **PASS (with BUG-3, BUG-4)** |
| 1.1b `.chronos/` contents | `rules/`, `logs/`, `config.json` | all three present. `traces/` absent (created lazily by the pytest plugin — acceptable). | **PASS** |
| 1.1c `pre-commit` contains enforce | yes | `python -m chronos enforce --repo "$(git rev-parse --show-toplevel)" --fail-on-block` | **PASS** |
| 1.1d `post-merge` contains index | yes | `python -m chronos index --repo "$(...)"` — **this command does not work**, see BUG-3 | **FAIL** |
| 1.1e hooks executable | `True` | **`False` for both** — see BUG-4 | **FAIL** |

### BUG-3 (severity: crash) — `init` writes a `post-merge` hook that always fails

The generated hook is:

```sh
python -m chronos index --repo "$(git rev-parse --show-toplevel)"
```

but `--repo` is only defined as a **subcommand** flag on `init` and `enforce`.
For `index` it must precede the subcommand (`chronos --repo X index`). Run
verbatim:

```
chronos: error: unrecognized arguments: --repo C:/.../MediAssist
EXIT=2
```

So every `git merge`/`git pull` in an initialized repo runs a hook that exits 2
and never re-indexes. `pre-commit` is unaffected (`enforce` defines its own
`--repo`), which is exactly why the inconsistency slipped through.

Fix: either emit `python -m chronos --repo "$..." index`, or add a subcommand
`--repo` to `index` for symmetry. The second is better — the current split
(global for some, subcommand for others) is the root cause of BUG-3 and 0.0.

### BUG-4 (severity: wrong, platform-dependent) — hooks written without the executable bit

```python
os.stat(pre-commit).st_mode & stat.S_IXUSR  ->  False
os.stat(post-merge).st_mode & stat.S_IXUSR  ->  False
```

Git silently **skips** a non-executable hook on Linux/macOS — no error, no
warning, enforcement just never runs. On Windows/msys git is more forgiving, so
this passes unnoticed here and fails on a partner's machine. `init` should
`chmod +x` after writing.

| 1.2 init again (idempotency) | no duplicate hooks | hooks stayed 2 lines each; re-ran cleanly. (No literal "MCP block already present" line — Claude Desktop config absent on this machine, so that path never ran.) | **PASS** |
| 1.3 init repo B (space in path) | same as 1.1 | `.chronos/` + hooks created. Hook quotes `"$(git rev-parse --show-toplevel)"`, so the space is safe. | **PASS** |
| 1.4 init on a non-git dir | warns, continues | `[5/6] not a git repo - skipping hooks`, `[!] not a git repo`, `.chronos/` still created, exit 0. | **PASS** |

---

## Phase 2 — Indexing

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 2.1 index A | nodes/edges, no errors | **2,424 nodes, 398 edges, 25.3s** (34s wall incl. sync). `added=376`. Matches STATUS's 2,415/398 row — indexer is stable run-to-run. | **PASS** |
| 2.2 index B | same | **730 nodes, 583 edges, 29.8s** (38s wall). `added=572`. Space in path handled. | **PASS** |
| 2.3 language populated | matches extension | A: 338 python / **2,086 unknown**. B: 411 python / 319 unknown. See NOTE-L below — mostly correct, one real gap. | **PASS (qualified)** |
| 2.4 qname uniqueness check | can verify no collisions | **Cannot be checked from the graph — `qname` is not stored.** See BUG-5. | **FAIL** |

### BUG-5 (severity: wrong) — `sync.py` drops `qname`; it is never persisted to the graph

`chronos/sync.py:188-191`:

```python
# attributes is an explicit list, so a new node field must be
# added here or it is silently dropped on the way to the graph.
attributes={"path": n.get("path", ""), "kind": n.get("kind", "Symbol"),
            "language": n.get("language", "unknown")},
```

`qname` is absent from that whitelist — the exact failure the comment above it
warns about. Measured:

- indexer supplies qname for **730/730** nodes in repo B (and all 2,424 in A);
- **0/2,424** and **0/730** nodes have qname in the graph afterwards.

Identity is **not** corrupted: `node_identity(n)` reads the pre-sync dict, so
UUIDs are still qname-derived and correct. The damage is that qname is
unqueryable after sync, so:

- Phase 2.4's collision check is impossible from the graph (had to be done on
  the indexer's output instead);
- anything wanting to look a node up by qualified name — the identity STATUS
  calls load-bearing — cannot.

STATUS documents this exact class of bug ("Both input paths must agree on
identity", where `indexer.py` dropped qname). It was fixed on the input side;
the same field is still dropped on the **output** side. The round-trip test
(`roundtrip()`) passes because it re-derives identity the same way, so it
cannot see this.

Fix: add `"qname": n.get("qname", "")` to the attributes dict.

### NOTE-L — 86% `unknown` language in repo A is mostly correct, but hides one gap

Repo A: 1,791 of 2,424 nodes are `kind: Variable`, plus `File`/`Section`/
`Module`/`Project`/`Branch` nodes. `node_language` derives from the file
extension, and these node kinds carry `path: "{}"` (see NOTE-P), so `unknown`
is the honest answer for most of them — STATUS's Wedge 4 note says exactly this
("643 Folder/Module/Project nodes correctly `unknown`, never `None`").

The gap: **`File` nodes for non-Python source get `unknown` too.** Repo B has
`Index.html` → `unknown`, because `EXTENSION_TO_LANGUAGE` has no `.html`. Any
rule scoped to a language other than the handful mapped will silently match
nothing. Not a crash — a silent coverage hole.

### NOTE-P — `path` is the literal string `"{}"` for non-file nodes

`{"path": "{}", "kind": "Project", ...}`. This comes from **upstream's**
indexer, not Chronos — verified by reading `index_repo_graph()` output directly
before sync. Cosmetic, but it means `summary` (set to `n["path"]`) is the
two-character string `{}` for those nodes rather than empty.

| 2.5 re-index A (idempotency) | same count, no dupes | `2,424 nodes`, `added=0 invalidated=0 unchanged=376`, 17.7s. Exact idempotency. | **PASS** |
| 2.6 index an empty subdir | graceful | `2 nodes, 0 edges`, exit 0. Skeleton Project/Branch nodes only. | **PASS** |

---

## Phase 3 — Wedge 1 (Temporal Graph)

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 3.1 query real symbols | facts with timestamps | `run_benchmark` → 1 caller; `__init__` → "existed but had no callers"; both plausible. | **PASS** |
| 3.2 query nonexistent file | empty / not-found, no crash | `"symbol 'src/does_not_exist.py' is not present in the graph for any time period"` — distinct from the "existed but no callers" message. P0-4 contract holding. | **PASS** |
| 3.3 gc / orphans | 0 or small on fresh index | `0 orphaned nodes of 2424 (0.0%)`, `nothing to collect.` | **PASS** |
| 3.4 supersession on edit | new fact `valid_at` >, old gets `invalid_at` | Structural edit → `added=1`; removing the call → `invalidated=1`, `invalid_at` set, current query drops to 0 callers, superseded edge still queryable. **But `valid_at == invalid_at`** — see BUG-7. | **PASS (mechanism) / FAIL (timestamps)** |
| 3.5 query speed ×10 | <100ms | min 43.5 / max 65.5 / **mean 53.6 ms**. Inside the plan's bar, but ~2.7× the validation report's 20ms mean. | **PASS (slower than recorded)** |

Comment-only edit → `added=0 invalidated=0`: correct, a comment changes no AST
structure. Supersession was therefore tested with a real structural edit
(two new functions + a call edge, then removing the call).

### BUG-6 (severity: wrong — silently wrong history) — commit timezone offset is dropped, not converted

`cli.py:32 commit_time()` returns `datetime.fromisoformat("2026-08-05T19:48:20+05:30")`
— correctly tz-aware. But the value stored in and read back from the graph is:

```
git   : 2026-08-05T19:48:20+05:30   (= 14:18:20 UTC)
stored: 2026-08-05 19:48:20         (naive — offset dropped)
read  : 2026-08-05T19:48:20+00:00   (re-interpreted as UTC)
```

The wall-clock digits are kept and the offset is discarded, so the fact is
recorded **5h30m later than it actually happened** (worse for larger offsets).
Any `as_of` query landing in that window gets the wrong answer — and gets it
confidently, since no error is raised. For a bi-temporal graph whose entire
value proposition is "what was true at time T", this is the most serious
finding in this run.

Reproduce: index any repo whose HEAD commit is in a non-UTC zone, then compare
`git log -1 --format=%cI` against `min(e.valid_at)` in the graph.

### BUG-7 (severity: wrong) — `valid_at == invalid_at` when syncing an uncommitted working tree

`commit_time()` uses **HEAD's** commit time so that re-syncing an old checkout
doesn't claim to be current — sound design. The side effect: when you edit files
**without committing** and re-index (exactly what a `post-merge`/watch workflow
does, and what an agent does mid-task), every sync stamps the *same* HEAD
timestamp. Observed:

```
edge chronos_pilot_marker_fn -> chronos_pilot_helper
  valid_at   2026-08-05 19:48:20
  invalid_at 2026-08-05 19:48:20
```

The fact was born and died at the same instant, so it is invisible to every
as-of query — there is no T where it was true. The temporal record of that
change is effectively lost.

Suggested fix: when the working tree is dirty (`git status --porcelain`
non-empty), stamp `max(HEAD_commit_time, now)` or `now`, since the change
demonstrably is not part of HEAD.

### BUG-8 (severity: cosmetic, but misleading) — "predates the earliest record" for a time inside the range

Querying as-of exactly the earliest recorded instant returns:

> `requested time 2026-08-05T19:48:20+05:30 predates the graph's earliest record (2026-08-05T19:48:20+00:00)`

The two timestamps print as the same wall clock and the message claims one
predates the other. This is BUG-6's offset drop surfacing in a user-visible
message; it reads as a Chronos bug to anyone debugging.

---

## Phase 4 — Wedge 3 (Coordination)

Plan names `chronos.ledger.acquire_lock`; the real API is `ledger.acquire()` /
`ledger.release()`, wrapped by MCP-level `chronos_acquire_lock` /
`chronos_release_lock` in `wedge3_mcp.py`. Tests used the MCP-level functions
(the names the plan intends), called in-process.

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 4.1 acquire lock | `acquired: true` | `{"acquired": true, "renewed": false, "expires_at": "...17:42:48Z"}` | **PASS** |
| 4.2 same node, other agent | `acquired: false`, `held_by` | `{"acquired": false, "reason": "conflict", "conflict": {"agent_id": "test-agent-alpha", "intent": "pilot 4.1", ...}}` — reports holder **and** intent. | **PASS** |
| 4.3 release then re-acquire | `acquired: true` | released `true`, beta then acquired `true`. | **PASS** |
| 4.4 conflicts across 3 locked + 2 free | 3 contested, 2 clean | `{checked, locked, free, conflict_count}` — 3 in `locked` with holder/intent, exactly the 2 unlocked in `free`. (Key is `locked`, not `conflicts`.) | **PASS** |
| 4.5 log provenance + who_touched | ≥1 event | `{"logged": true, "id": 1}`; `who_touched` → 1 event with agent, action, reason, timestamp. | **PASS** |
| 4.6 release a lock that isn't held | graceful | `{"released": false, "reason": "not_locked"}` — no crash, no silent success. | **PASS** |
| 4.7 lock a node absent from the graph | document behavior | **Acquires successfully.** Locks are intentionally graph-independent. | **PASS (correct)** |

**4.7 is the right behavior.** The ledger is a separate SQLite store keyed on
arbitrary node ids; requiring graph presence would make locking fail exactly
when an agent is creating a *new* symbol — the case most in need of
coordination. Cost: a typo'd node id locks silently and protects nothing. Worth
documenting, not changing.

**Observed side effect:** Trigger 3 (lock conflict → coordination-lesson hint)
fired on 4.2 and printed a multi-line suggestion to stderr mid-JSON. Correct
per STATUS (hint only, no reflection), but it interleaves with tool output.

---

## Phase 5 — Wedge 4 (Enforcement)

Repo A has no `requests`/`httpx`, so the plan's suggested "API without timeout"
rule would have matched nothing. Used a real, verifiable pattern instead:
`print()` in `src/` (present in many modules).

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 5.1 generate rule (gpt-oss-120b) | YAML stored, CHECK A/B | CHECK A **passed**, CHECK B **failed** — `"the rule does not match its own positive example (zero matches)"`. Pattern emitted was `print($ARGS..)`. See BUG-10. | **PASS (gate worked)** |
| 5.1b same rule, minimax-m3 | — | Pattern `print($$$)`, CHECK B passed (`matched 3`), status `warn-only-validated`. | **PASS** |
| 5.2 non-automatable rule | `NOT_AUTOMATABLE`, no broken YAML | `automatable: false`, reason retained, **no `.yml` written**. | **PASS** |
| 5.3 list rules | rule appears warn-only-unvalidated | both rules present with correct statuses. | **PASS** |
| 5.4 enforce on a matching file (MCP) | WARN + rule id + location | `warn`, `matched_node: "print(res)"`, `[explain_prediction.py:64]`, `rule_status: warn-only-validated`. Broken rule correctly `pass / no matches`. | **PASS** |
| 5.5 enforce on non-matching file | no violations | both rules `pass / no matches`. | **PASS** |
| 5.6/5.7 promote | → validated → blocking | Generated rules enter at `warn-only-validated`, so promotion is **one** step to `blocking`, not two. (`approve-rule` covers proposed→unvalidated for git-native rules.) | **PASS (fewer steps than plan)** |
| 5.6b promote a CHECK-B-failed rule | refused | `"detectability did not pass — cannot promote an unvalidated rule to blocking"`, twice. Cannot reach blocking. | **PASS** |
| 5.8 enforce a BLOCKING rule | BLOCK + provenance stamp | **`warn`**, message `"pattern matched but graph does not confirm deprecation"`, no provenance stamp. **This is correct** — OPA requires promotion **and** graph-confirmed deprecation. `print` is not deprecated. Warns are never stamped, as designed. | **PASS (plan's expectation was wrong)** |
| 5.9 CLI enforce vs MCP enforce | same verdict | **CLI says `OK / 0 warn`; MCP says `warn` — same file, same rule.** See BUG-9. | **FAIL** |

### BUG-9 (severity: crash-class silent false negative) — CLI `enforce` reads a different rule store than the MCP tools

`do_enforce` calls `load_repo_config(repo)` (`cli.py:300`), which sets
`CHRONOS_SQLITE` to the **target repo's** `.chronos/chronos.db`. But every rule
created through the MCP tools lands in the **global** `~/.chronos/chronos.db`.
Measured:

```
MCP  enforce(explain_prediction.py) -> pilot-print-2: warn   (blocking rule matched)
CLI  enforce --file <same file>     -> OK / 0 block, 0 warn, 1 ok

MediAssist/.chronos/chronos.db  ->  enforcement_rules = []      (0 rules)
~/.chronos/chronos.db           ->  pilot-print-2 = blocking
after load_repo_config(A): rule_store.get_active_rules('python') -> []
```

The CLI is the path CI and the `pre-commit` hook use. It reports a clean pass on
a file that violates a **blocking** rule, with exit 0 — a silent false negative
in the gate whose entire purpose is to not be silent. Anyone generating rules via
MCP and enforcing via CI gets no enforcement at all, and nothing warns them.

Related: `chronos doctor` reads the global store too, so `enforce: ok | 1
blocking` is reported while the repo's own store has none.

Fix direction: one store per repo, chosen the same way by every entry point —
or have `init` seed the repo config from the global path. Either way the two
paths must agree.

### BUG-10 (severity: wrong, model-dependent) — LLM emits invalid ast-grep metavariable syntax, and ast-grep accepts it silently

`gpt-oss-120b` generated `pattern: print($ARGS..)`. Verified directly against
ast-grep 0.45.1:

```
pattern: print($ARGS..)   -> 0 matches, exit 0
pattern: print($$$ARGS)   -> 1 match,  exit 0
```

ast-grep does **not** reject `$ARGS..` — exit 0 either way — so CHECK A (syntax,
which reads the exit code) passes and only CHECK B (self-match) catches it. This
is precisely the confusion STATUS's Wedge 4 research section warns about, and it
is the reason CHECK B has to exist. Working as designed, but worth recording:

- rule quality is **model-dependent** (`minimax-m3` got it right, `gpt-oss-120b`
  did not, same prompt);
- a CHECK-B-failed rule is still stored and still appears in
  `get_active_rules()`, so it is live in the warn path while matching nothing —
  a rule that looks active but is inert. It cannot be promoted, so it can never
  block; the risk is false confidence, not false blocking.
| 5.10 rule report | summary of matches | Works with **no args**: `{total_checks:0, blocks:0, warns:null, passes:null, note:"..."}`. **Takes no rule_id** — it is a global audit, not per-rule. Passing one crashes: see BUG-11. | **PASS (plan assumed wrong signature)** |
| 5.11 enforce on a binary file | skip / unsupported, no crash | `pass / no matches` on a `.db` file. No crash, no garbage match. | **PASS** |
| 5.12 enforce repo B | warn-only or 0, no crash | `Checked 4 files - 0 block, 0 warn, 4 ok`, exit 0. | **PASS** |

### BUG-11 (severity: crash) — `chronos_rule_report` crashes on a non-int argument

Signature is `chronos_rule_report(since_days: int = 30)`. Passing a string
(e.g. a rule id, the natural mistake given the tool's name) raises:

```
TypeError: unsupported type for timedelta days component: str
  at wedge4_mcp.py:87
```

An MCP tool should not raise a raw TypeError at an LLM caller — models routinely
pass a plausible-looking wrong argument. Validate/coerce `since_days`, or accept
an optional `rule_id` since the name invites it.

---

## Phase 6 — Wedge 2 (Playbook Learning)

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 6.1 pytest capture in repo A | `pending.jsonl` with `total_failed>=1` | **Nothing written on the first attempt** — plugin not registered. After `pip install -e .`: trace written, `total_failed: 1`, `test_id`, message `AssertionError: pilot test`, 160-char traceback. See BUG-12. | **FAIL then PASS** |
| 6.2 bash hook on exit-0 with failure text | hook fires | Fired; wrote 577 bytes to MediAssist's `pending.jsonl`. (Plan's `~/.claude/hook-payloads.jsonl` doesn't exist — that was a one-off investigation log, not a shipped artifact; the real target is `<repo>/.chronos/traces/pending.jsonl`.) | **PASS** |
| 6.3 process_pending | dispatched, file cleared | `dispatched: 1`, `pending.jsonl` → 0 bytes. | **PASS** |
| 6.4 playbook health | backend + dirs + proposed | `{status: ok, total: 3, proposed: 1, blocking: 1, warn_only: 1, rule_backend: "git-native", packmind_url: null, git_native_rules_dir: "..."}` | **PASS** |
| 6.5 query playbook | matches or empty, no crash | `"error handling"` → `[]`; empty query → all 3 rules. No crash. Filtering is literal substring, not semantic — STATUS documents this ("No semantic search"). | **PASS** |
| 6.6 malformed JSON line | skipped, valid still processed | malformed line dropped, the valid trace still dispatched, file cleaned. | **PASS** |
| 6.7 trace older than 24h | discarded, not dispatched | `dispatched: 0`, file cleaned. | **PASS** |

### BUG-12 (severity: crash-class silent no-op) — the pytest plugin was never registered; automatic capture was dead in every repo

First run of 6.1 in repo A produced **no trace at all**. Cause:

```
installed entry_points.txt : [console_scripts] only — no [pytest11] section
  mtime 2026-08-13 17:18
pyproject.toml (has pytest11) mtime 2026-08-14 00:38
pytest11 entry points seen  : ['anyio','hypothesispytest','pytest_cov','xdist',...]  # no chronos
```

The `pytest11` entry point was added to `pyproject.toml` **after** the last
install, and the editable install was never refreshed. An editable install does
not regenerate `entry_points.txt` on source changes — only on reinstall.

Why this went unnoticed: `New ortho/conftest.py` sets
`pytest_plugins = ["chronos.pytest_plugin"]`, so capture worked **in the Chronos
repo only**. Its own comment names the very assumption that failed:

> "For partner repos: they either copy this conftest.py or install chronos as a
> package, which registers the plugin via entry_points automatically."

So STATUS's "Automatic capture — LIVE-VERIFIED, real failing suite → real trace"
is true in the Chronos repo and was **false in every other repo** on this
machine. `pip install -e .` fixes it (`pytest11` now lists `chronos`), and 6.1
then passed in repo A with no conftest.

This is the highest-impact finding after BUG-9: the wedge's entire input path
was silently inactive for partner repos, with no error, no warning, and a green
test suite.

Suggested guard: have `chronos doctor` check
`importlib.metadata.entry_points(group="pytest11")` for `chronos` and report
`capture: INACTIVE — run pip install -e .` when absent.

---

## Phase 7 — Cross-wedge triggers

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 7.1 Trigger 1 latency (cold process) | <500ms | **5,175 ms** — but see NOTE-C: this is one-time process startup, not the trigger. | **PASS (measurement artifact)** |
| 7.1b Trigger 1 latency (warm driver, real block) | <500ms | triggers **on**: 434/442/479 ms · **off**: 287/355/363 ms. Delta ≈100ms = thread spawn, not the 5s Reflector. | **PASS** |
| 7.1c Reflector actually runs behind the block | runs async | `auto-trigger: block on pilot-block -> reflector produced rule 'IF code calls chronos_pilot_helper THEN flag it as deprecate'`, `drain(20) -> 1`. | **PASS** |
| 7.2 "rule change → re-index" | re-index fires | **No such trigger exists.** The three are `on_block` (4→2), `on_deprecation` (1→4), `on_conflict` (3→2) — as STATUS documents. Plan's premise is wrong. | **N/A** |
| 7.2b real Trigger 2 (`on_deprecation`) | warn when uncovered | covered → returns `pilot-block` at INFO; uncovered → `WARNING ... no enforcement rule covers it. Run chronos_generate_rule`. | **PASS** |
| 7.3 `CHRONOS_AUTO_TRIGGERS=0` | no background activity | `enabled(): False`; `on_block`, `on_deprecation` both return `None`. Kill switch works. | **PASS** |

### NOTE-C — the 5.2s enforce was process startup, not the enforcement path

Cold process: 5,175 ms (triggers on) / 4,474 ms (triggers off) — the ~700ms gap
is not the Reflector either. With the Kuzu driver already open:

```
enforce, warm driver: 476 / 297 / 285 ms   (2 rules, real ast-grep + real OPA)
```

The 4+ seconds is Python import + Kuzu driver open + schema, paid once per
process. `wedge4_mcp.driver()` caches `_driver` globally, so a long-lived MCP
server pays it once at startup; the CLI pays it on every invocation. Worth
knowing for the `pre-commit` hook, which is a fresh process every commit —
**every commit costs ~5s** even when nothing matches.

STATUS's 28ms figure measures the enforce path with everything warm and is not
contradicted; 285ms here is 2 rules × ast-grep subprocess + OPA subprocess.

### A real BLOCK was produced and verified (extends 5.8)

5.8 could only reach `warn` because no matched symbol was deprecated. Forcing
the other half of the OPA condition — a rule on `chronos_pilot_helper`, whose
call edge I superseded in 3.4 — produced a genuine block:

```json
{"rule_id": "pilot-block", "verdict": "block",
 "matched_qualified_name": "chronos_pilot_helper",
 "deprecated_since": "2026-08-05T19:48:20",
 "rule_status": "blocking", "provenance_event_id": "2",
 "message": "pattern matches deprecated node confirmed by temporal graph [blocktest.py:2]"}
```

Both OPA conditions (human promotion **and** graph-confirmed deprecation) are
required and both were exercised. Provenance stamped. This is the strongest
end-to-end evidence in this run.

---

## Phase 8 — Dashboard

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 8.1 `/health` | `{"ok": true}` | `{"ok":true}` | **PASS** |
| 8.2 `/api/stats` | nodes>0, ints | `{"total_nodes":3759,"active_locks":0,"rules_total":4,"violations_today":10}` | **PASS** |
| 8.3 `/api/rules` | rule present, fired_count≥1 | `pilot-block` blocking **fired_count 10**; all 4 rules listed with status + created_at. | **PASS** |
| 8.4 `/api/churn` | files with touch counts | `chronos_pilot_helper` touch_count 10, agents `["p","pilot"]`; `clean_text::Function` 1 (the 4.5 provenance event). Top entry is genuinely what this session touched. | **PASS** |
| 8.5 `/api/timeline` | 14 days, today>0 | 14 rows `2026-08-01..08-14`, today `{"blocked":10,"warned":0}`. | **PASS** |
| 8.6 `/api/queue` | proposed rules appear | Returns `pilot-no-print` (`warn-only-unvalidated`). **`proposed` rules do NOT appear** — see NOTE-Q. | **UNEXPECTED** |
| 8.7 dashboard HTML | loads | `GET / -> 200`, serves `dashboard.html` with palette + title. (No browser here; charts not visually verified.) | **PASS (partial)** |
| 8.8 empty data | zeros/empty, no 500 | All endpoints 200: `rules_total:0`, `[]` for rules/queue/churn. No 500s. | **PASS** |

Cross-check: the 10 blocks appear consistently in `stats.violations_today`,
`rules.fired_count`, `churn.touch_count` and `timeline[today]` — four
independent views agreeing, which is a genuine integration signal.

### NOTE-Q — `/api/queue` shows `warn-only-unvalidated`, not `proposed`

```python
# dashboard_server.py:188
"FROM enforcement_rules WHERE status = ?", ("warn-only-unvalidated",)
```

The queue is "awaiting validation", not "awaiting approval". The git-native
`proposed` rule (`chronos-2560a2b7`) is therefore **invisible in the review
queue** — the one state that explicitly needs a human decision (`approve-rule`)
has no dashboard surface. Given dual-path distribution made `proposed` the entry
state, the queue arguably should show both.

### NOTE-G — `total_nodes` is global, not per-repo

`total_nodes: 3759` = 2426 (mediassist) + 730 (cooling) + 601 (New ortho) + 2
(emptytest). It stayed 3759 even when the server was pointed at an empty
`CHRONOS_SQLITE`, because node counts come from the Kuzu graph, which that env
var does not repoint. Same root cause as BUG-1: one global store, no repo
scoping.

---

## Phase 9 — Edge case battery

| TEST | EXPECTED | ACTUAL | STATUS |
|---|---|---|---|
| 9.1 deep nested path (73 chars) | no crash | enforce ran, `print()` in it correctly warned. | **PASS** |
| 9.1b path >255 chars (261) | no crash | created and enforced cleanly. | **PASS** |
| 9.2 unicode filename | no crash | `módulo_핵심.py` (Latin-1 accents + Hangul) created, indexed, enforced. | **PASS** |
| 9.3 concurrent lock race (5 threads) | exactly 1 acquired | **`acquired: 1  rejected: 4`** | **PASS** |
| 9.4 trailing-garbage DB | error, non-zero | **No error** — SQLite tolerates trailing bytes after the last page; this is not corruption. Not a Chronos fault. | **N/A (invalid test)** |
| 9.4b genuinely corrupt DB (clobbered header) | error, non-zero | `sqlite3.DatabaseError: file is not a database`, **exit 1**. Loud, never silently successful. Raw traceback rather than a friendly message (cosmetic). | **PASS** |
| 9.5 read-only `.chronos/` | clear permission error | **SKIPPED** — `icacls /deny` reported `Failed processing 1 files`, so write access was never actually revoked and the index succeeded. Untested, not passed. | **SKIPPED** |
| 9.6 rapid re-index x5 | all succeed, stable count | stable at **2,440 nodes**; first run `invalidated=3` (my deleted test files), then `added=0 invalidated=0` x4. No lock errors, no duplicates. | **PASS** |
| 9.7 doctor both repos | report state | See below — BUG-1 in its clearest form. | **FAIL** |

### 9.7 — BUG-1 at its worst: half the output is right, half is wrong

`doctor` for **repo B** printed:

```
upstream db : ...C-Users-...-MediAssist.db      <- repo A's database
upstream    : 2440 nodes, 398 temporal edges    <- repo A's numbers
chronos     : stale | 730 nodes | 572/572       <- repo B's numbers (follows --group)
database    : C:\Users\urbra\.chronos\chronos.db | 11 events | 4 rules   <- global, same for both
```

The `chronos:` line honours `--group` while the `upstream:` line ignores
`--repo`, so a single report mixes two repos' data with no indication. That is
more dangerous than being uniformly wrong: the numbers look self-consistent.

---

# PHASE 10 — FINDINGS REPORT

## Summary

**Total tests: 62** (the plan's items, plus follow-ups where a result needed
isolating — 9.4 split into two, 7.1 into cold/warm)

| | Count |
|---|---|
| **PASS** | 46 |
| **FAIL** | 8 |
| **UNEXPECTED** | 2 |
| **N/A (plan premise wrong / invalid test)** | 5 |
| **SKIPPED** | 1 |

## Bugs Found

| ID | TEST | WHAT HAPPENED | EXPECTED | SEVERITY |
|---|---|---|---|---|
| **BUG-9** | 5.9 | CLI `enforce` loads the target repo's `.chronos/config.json`, repointing `CHRONOS_SQLITE` to a repo-local DB with **0 rules**, while MCP tools write to the global `~/.chronos/chronos.db`. CLI reported `OK / 0 warn` on a file the MCP path flagged `warn` against a **blocking** rule. | same verdict from both paths | **crash-class silent false negative** |
| **BUG-12** | 6.1 | pytest plugin never registered — installed `entry_points.txt` had no `[pytest11]` section (written Aug 13 17:18; `pyproject.toml` gained it Aug 14 00:38). Automatic capture was **dead in every repo except Chronos itself**, which has a `conftest.py`. No error, no warning. | trace captured | **crash-class silent no-op** |
| **BUG-6** | 3.4 | Commit timezone offset **dropped, not converted**: git `19:48:20+05:30` (= 14:18:20Z) stored naive and re-read as `19:48:20+00:00` — facts recorded 5h30m late. | correct instant | **wrong (silent)** |
| **BUG-3** | 1.1d | `init` writes a `post-merge` hook running `chronos index --repo <path>`; `index` has no subcommand `--repo`, so the hook exits 2 on every merge/pull. | hook re-indexes | **crash** |
| **BUG-5** | 2.4 | `sync.py:190` builds `attributes` as an explicit whitelist omitting `qname` — the exact failure its own comment warns about. Indexer supplies qname for 730/730 nodes; **0/730** have it in the graph. | qname queryable | **wrong** |
| **BUG-1** | 0.1, 9.7 | `doctor` is repo-blind: `find_db()` returns the **newest DB in a global cache by mtime**, ignoring `--repo`. Repo B's report showed repo A's database and node counts. | per-repo report | **wrong** |
| **BUG-4** | 1.1e | Git hooks written **without the executable bit**. Git silently skips non-executable hooks on Linux/macOS — enforcement never runs, no warning. | `chmod +x` | **wrong (platform-dependent)** |
| **BUG-11** | 5.10 | `chronos_rule_report("some-rule-id")` raises `TypeError: unsupported type for timedelta days component: str`. Param is `since_days: int`; the tool's name invites passing a rule id. | validation or accept rule_id | **crash** |
| **BUG-7** | 3.4 | Syncing an **uncommitted** working tree stamps HEAD's time for every sync, so a fact created and superseded between commits gets `valid_at == invalid_at` — invisible to every as-of query. | distinct timestamps | **wrong** |
| **BUG-10** | 5.1 | `gpt-oss-120b` emitted `print($ARGS..)` (invalid metavar syntax). ast-grep exits **0** either way, so CHECK A passed and only CHECK B caught it. Rule stays in `get_active_rules()` while matching nothing. | — | **wrong (model-dependent)** |
| **BUG-8** | 3.4 | "requested time X predates the graph's earliest record (X)" — same wall clock on both sides. Surface of BUG-6. | coherent message | **cosmetic** |

## Edge Cases That Need Fixes

| EDGE CASE | CURRENT | CORRECT |
|---|---|---|
| Rules created via MCP, enforced via CLI/CI | two different stores, silent pass | one store per repo, chosen identically by every entry point |
| Chronos installed but not reinstalled after `pyproject` change | capture silently inactive | `doctor` checks `entry_points(group="pytest11")` and reports `capture: INACTIVE` |
| `doctor --repo X` | reports whichever DB is newest globally | derive the cache DB from the repo path; say "not indexed" if absent |
| `post-merge` hook | exits 2 every merge | emit `chronos --repo X index`, or give `index` a subcommand `--repo` |
| Hooks on Linux/macOS | silently skipped (no +x) | `os.chmod(h, 0o755)` after write |
| Non-UTC commit timezone | facts stored at wrong instant | store UTC-normalized, tz-aware |
| Dirty working tree re-index | `valid_at == invalid_at` | stamp `now` when `git status --porcelain` is non-empty |
| `.html` / unmapped extensions | `unknown`, so all rules skip the file | extend `EXTENSION_TO_LANGUAGE`, or warn when a rule's language never matches anything |
| `/api/queue` | shows only `warn-only-unvalidated` | include `proposed` — the state that actually needs a human |
| MCP tool arg validation | raw `TypeError` reaches the caller | coerce/validate; return a structured error |

## Unexpected Behaviors (not bugs)

| WHAT | WHY IT MATTERS |
|---|---|
| **5.8 blocking rule returned `warn`** | Correct, not a bug: OPA requires promotion **and** graph-confirmed deprecation. The plan expected BLOCK. Forcing both conditions later produced a real block with a provenance stamp. Enforcement is genuinely toothless without a graph in CI — as STATUS states. |
| **`enforce` prints `Checked 0 files`** when every changed file is an unmapped type | Indistinguishable from "enforce is broken". Report skipped counts. |
| **Cold-process enforce is 4.5–5.2 s** | Not the Reflector — Python import + Kuzu open + schema. The `pre-commit` hook is a fresh process, so **every commit pays ~5s**. Warm: 285–476 ms. |
| **Rule quality is model-dependent** | Same prompt: `minimax-m3` produced valid `print($$$)`; `gpt-oss-120b` produced invalid `print($ARGS..)`. CHECK B is the only thing standing between a bad model and a dead rule. |
| **`chronos_query_timeline` / `chronos_gc` do not exist** | The plan referenced them in 6 tests. Real tools: `as_of_callers/callees/impact`, `what_changed`, `index_health`; gc is CLI-only. |
| **Locks accept nodes absent from the graph** | Correct — a new symbol needs coordination before it exists. Cost: a typo'd id locks nothing real. |
| **Trigger 3 prints to stderr mid-tool-output** | Interleaves with JSON results; cosmetic but noisy. |

## Performance Notes

| Metric | Repo A (MediAssist) | Repo B (Cooling project) |
|---|---|---|
| Index | **2,424 nodes / 398 edges / 25.3 s** (34 s wall) | **730 nodes / 583 edges / 29.8 s** (38 s wall) |
| Re-index (no change) | 17.7 s, `added=0 invalidated=0` | — |
| Rapid re-index x5 | stable 2,440 nodes, no drift | — |
| Query (`callers`) x10 | min 43.5 / max 65.5 / **mean 53.6 ms** | — |
| Enforce (warm driver, 2–3 rules) | 285–476 ms | — |
| Enforce (cold process) | 4,474–5,175 ms | — |
| Enforce block path, triggers on/off | 434–479 ms / 287–363 ms (~100 ms trigger overhead) | — |

Query mean 53.6 ms is ~2.7x the validation report's 20 ms, still well inside the
sub-second p95 target. Index throughput matches STATUS's recorded MediAssist row
(2,415/398) almost exactly.

## What Worked Correctly

- **Wedge 3 (coordination) — flawless.** Every one of 4.1–4.7 passed: conflict reports holder and intent, a 5-thread race yielded exactly 1 winner, `not_locked` handled gracefully, provenance append-only.
- **Wedge 1 temporal mechanics** — supersession sets `invalid_at`, current queries drop the edge, superseded facts stay queryable, gc reports 0 orphans on a fresh index, incremental sync is exactly idempotent.
- **The no-data contract (P0-4)** — "not present in the graph" vs "existed but had no callers" are distinct messages. No silent empty results anywhere.
- **Wedge 4 detectability gate** — CHECK B caught an invalid pattern that CHECK A and ast-grep both accepted; promotion of a failed rule was refused twice; NOT_AUTOMATABLE wrote no YAML.
- **A real end-to-end BLOCK** with both OPA conditions satisfied and a provenance stamp written.
- **Trigger 1 async fix holds** — a block returns in ~450 ms with the Reflector running behind it (`drain -> 1`), confirming the 38x regression stays fixed.
- **Trace hygiene** — malformed JSON skipped, >24 h traces discarded, clean runs write nothing, file cleaned after drain.
- **Dashboard** — all 7 endpoints returned coherent, mutually consistent data; empty DB gives zeros, never 500s.
- **Robustness** — 261-char paths, unicode filenames, binary files, empty dirs, paths with spaces: all handled without a crash.

## Test artifacts cleaned up

Pilot rules (`pilot-no-print`, `pilot-print-2`, `pilot-block`) deleted from
`enforcement_rules` and `.chronos/rules/`; pilot provenance events and locks
removed; test files, deep directories and the unicode file deleted from repo A;
both dashboard servers stopped. `.chronos/` and git hooks were left in place in
both repos (created by `init`, part of the tested state).

---

# FIX PASS — 2026-08-14

All findings resolved or explicitly withdrawn. Suite: **42 -> 61 passed**
(19 new regressions in `tests/test_pilot_regressions.py`), 6 standalone suites
still green.

| ID | Fix | Verified by |
|---|---|---|
| **BUG-9** | Store resolution moved into `db.db_path()`, which now reads the active repo's `config.json` (via `CHRONOS_REPO_PATH`) so **every** entry point resolves the same database. `load_repo_config` publishes the repo. | A rule written to the repo store is now seen by the CLI: `WARN ... rule:bug9-check` where it previously printed `OK / 0 warn`. |
| **BUG-12** | `doctor` gained a `capture:` line checking `entry_points(group="pytest11")`, so a stale editable install is visible instead of silently dead. Root cause was a stale `entry_points.txt`, fixed by reinstalling. | `capture : ok \| pytest plugin registered`; INACTIVE branch prints the `pip install -e .` remedy. |
| **BUG-6** | `commit_time()` calls `.astimezone(timezone.utc)` — converts instead of truncating. | Clean repo with a `+05:30` commit now stamps `2026-08-05T14:18:20+00:00` (was `19:48:20+00:00`, 5h30m in the future). |
| **BUG-7** | `commit_time()` stamps `max(HEAD, now)` when `git status --porcelain` is non-empty; HEAD's time still used for a clean tree. | Dirty tree stamps now; clean tree still returns the Aug 5 commit time. |
| **BUG-3** | `--repo` added as a subcommand flag to `index`, `sync`, `watch`, `health`, `doctor` (matching `enforce`/`init`), so both flag positions work. | `chronos index --repo <A>` exits 0 (was exit 2). Parametrized test covers all five. |
| **BUG-5** | `"qname"` added to `sync.py`'s attributes whitelist. | 400/400 nodes now carry qname (was 0/730); re-sync stayed `added=0 invalidated=0`, so identity did not churn. |
| **BUG-1** | `find_db(repo=...)` derives upstream's cache filename from the repo path; returns `None` rather than another repo's index. `doctor`/`health`/`_open_upstream` pass the repo. | Repo A shows MediAssist's DB (2440 nodes), repo B shows Cooling's (730). Unindexed repo: `NOT INDEXED -- run: chronos --repo ... index`. |
| **BUG-11** | `chronos_rule_report` coerces/validates `since_days` and returns a structured error. | Rule id -> error naming the mistake; `-1` -> error; `"7"` -> coerced and works. |
| **BUG-10** | Generator prompt now documents ast-grep metavariable syntax (`$$$` vs the invalid `$ARGS..`). | **gpt-oss-120b — the model that failed before — now emits `print($$$)` and passes CHECK B.** |
| **BUG-8** | Resolved by BUG-6; the message compared a truncated timestamp against a converted one. | No code change beyond `commit_time`. |
| **NOTE-Q** | `/api/queue` now returns `proposed` **and** `warn-only-unvalidated`, with `status` on each row. | Queue shows `chronos-2560a2b7 -> proposed`, previously invisible. |
| **NOTE-L** | `.html/.htm/.css/.json/.yaml/.yml/.php/.scala/.lua/.ex/.exs` added to `EXTENSION_TO_LANGUAGE`. `.scss` deliberately excluded — ast-grep 0.45.1 rejects it. | Enforce on repo A's last commit went from `Checked 0 files` to `Checked 17 files`. |
| **Enforce silent skip** | Verdict line reports skipped files. | `Checked 17 files - 0 block, 0 warn, 17 ok (9 skipped: no rules for that file type)` |

## BUG-4 — WITHDRAWN (false positive)

`init` already calls `p.chmod(p.stat().st_mode | 0o755)`. The bit did not appear
because **Windows `os.chmod` cannot set the POSIX execute bit** — verified
directly: a fresh file stays `0o100666` after the identical chmod. The code is
correct and will work on Linux/macOS, where the bit matters. Hooks live in
`.git/hooks`, which is never tracked, so there is no path by which a
Windows-created hook reaches a POSIX machine. The hook also runs correctly here.
No fix needed; the original finding was an artifact of testing on Windows.

## Regression introduced and fixed during the fix pass

Reinstalling to register the entry point (BUG-12) made the plugin load **twice**
in this repo — once via `pytest11`, once via `conftest.py` — and pluggy aborts
the whole session on a duplicate module (`Plugin already registered under a
different name`). This broke all 42 tests.

Fixed by removing the now-redundant `pytest_plugins` line from `conftest.py` and
from the three pytester tests, which now exercise the **entry-point path that
partner repos actually use** — stronger coverage than before. A `hasplugin`
guard was added to `pytest_configure` for the double-registration case it *can*
catch, with a comment stating plainly that it cannot rescue the conftest case,
since pluggy rejects the duplicate before any Chronos code runs.

## Not fixed (deliberate)

- **Cold-process enforce ~5 s.** ~~Out of scope for this pass.~~ **FIXED 2026-08-15 by the daemon** (`chronos daemon start`): 5,466-8,736 ms -> 167-206 ms. Profiling showed the cost was the *import chain*, not the driver — `import chronos.cli` alone is 5.0s (graphiti_core pulls in openai + neo4j) while opening Kuzu on top of that is free. So the client had to avoid importing `chronos.cli` at all, not just avoid re-opening the graph.
- **Query mean 53.6 ms vs the report's 20 ms.** Well inside the sub-second target; no regression identified.
- **Rule quality is model-dependent.** The prompt fix helps; CHECK B remains the real guard, which is what it is for.
