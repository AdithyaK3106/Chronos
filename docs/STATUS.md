# Chronos — Status

**Updated:** 2026-08-14
**Scope:** All four wedges — 1 (Bi-Temporal AST Graph), 2 (Policy Playbook),
3 (Intent & Provenance Ledger), 4 (CI Enforcement).
**Verdict:** Wedges 1 and 3 are **functionally complete and verified end-to-end**
against four real third-party repos. Wedge 4 is **complete and verified against
the real ast-grep and OPA binaries**, with both validation-run blockers now
fixed; its CI workflow is now installed at `.github/workflows/` with two
pre-run faults fixed, but has still never run on GitHub Actions — this repo has
no git remote. Wedge 2 **no longer requires Packmind at all**: git-native
distribution is the default path and runs end-to-end against real git. Its
**Reflector is live-verified**, its HTTP client is now socket-verified against a
local fake, and **automatic failure capture works** via a pytest plugin —
and those captures are now **grounded**, with traceback frames resolved against
a real index instead of reaching the Reflector with no nodes. The **full chain
has now run end-to-end with nothing mocked** (capture → grounded reflect → live
quality gate → git-native proposed rule). What remains unproven is a live run
against a real Packmind instance — still blocked on a container runtime, not on
code — and the Curator's embedding dedup, for want of a reachable embeddings
model.

---

## Wedge 1 — P0 acceptance

Each row was executed, not reasoned about. Evidence is what the command actually printed.

| Req | Status | Evidence |
|---|---|---|
| **P0-1** Adopt upstream as parsing engine | ✅ | Built from vendored source (v0.10.3). Indexed 3 repos. Chronos re-parses nothing. |
| ↳ ingests parse success/failure counts | ✅ | `health.coverage` → `{parse_partial: 2, not_indexed_file: 7, not_indexed_dir: 7}` |
| ↳ no silent divergent fork | ✅ | Submodule pinned at `70a9539`; zero patches applied. |
| **P0-2** Sync layer → Graphiti bi-temporal | ✅ | Rename produced `added=4 invalidated=5 unchanged=178`. |
| ↳ superseded nodes stay queryable | ✅ | `callers(changes)` before rename → `[main, what_changed]`; after → none. |
| ↳ current queries never return invalidated edges | ✅ | Enforced in the query predicate; covered by the suite. |
| ↳ stateless, re-derivable glue | ✅ | Identity is `uuid5(group, qualified_name)`. No Chronos-side state exists. |
| **P0-3** Incremental re-index | ✅ | Unchanged re-index → `added=0 invalidated=0 unchanged=183`. |
| ↳ never requires full re-sync | ✅ | Same run: only the delta is written. |
| **P0-4** Unified MCP interface | ✅ | 5 tools live and answering. Current-state proxying intentionally not built — the PRD's open question on tool surface is answered in Decisions (D-1); scope superseded, not skipped. |
| ↳ explicit no-data, never silent fallback | ✅ | Pre-history → `"predates the graph's earliest record"`; unknown symbol → `"not present"`. |
| **P0-5** Self-hosted, bundled | ✅ | Embedded Kuzu, no server. Sync+query completed with **all external sockets raising**. |
| ↳ one install step | ✅ | `submodule update` → `pip install -e .` → `build_cbm` (verified from a fresh clone). |
| **P0-6** Index health | ✅ | `chronos health` returns status/freshness/coverage; exits 1 when not fresh. |

**Suite:** `python tests/test_chronos.py` → ALL PASS (17 checks, incl. supersession,
idempotency, identity collisions, no-data signalling, coverage, gc safety, and a
cross-path round-trip).

---

## Wedge 3 — Intent & Provenance Ledger

Agents declare intent on an AST node before touching it; every change is stamped
with who and why. Node ids are Wedge 1 identities, so a lock names the same symbol
the temporal graph does — **per-function, not per-file.**

| Capability | Status | Evidence |
|---|---|---|
| Intent locks, one per node | ✅ | PK-guarded INSERT in an IMMEDIATE transaction. 12 threads raced one node → exactly 1 acquired. |
| Conflict reports holder + intent | ✅ | agent-B on a held node → `held_by=agent-A, intent='refactor fallback loop'`. |
| TTL expiry, no background thread | ✅ | Swept on next acquire. Verified with a real 1s TTL elapsing. |
| Release restricted to owner | ✅ | Wrong agent → `not_owner` with current holder. |
| Append-only provenance | ✅ | Earlier rows verified unmutated after later appends. |
| Multi-node pre-flight | ✅ | `chronos_check_conflicts` dedupes and splits locked/free. |
| Doctor integration | ✅ | `ledger: ok \| N active locks \| N events \| <path>`. |

**Suite:** `python tests/test_wedge3.py` → ALL PASS (12 checks).
**Size:** 260 lines across `ledger.py` + `wedge3_mcp.py`.

Full two-agent scenario on real Opencode node ids (`newFallbackState::Function`):
A locks 1,2,3 → B conflicts on 2 → A logs provenance → A releases → B acquires →
`who_touched` shows A's change and reason. Ledger: 1 lock, 3 events.

**Scope limits, stated plainly:** `agent_id` identifies but does not authenticate —
any caller can claim any id, so this prevents collisions between *cooperating*
agents. And locks cover exactly the nodes named; structurally-adjacent conflicts
(two agents on functions that call each other) are not yet detected. Expanding the
lock set via `as_of_callers`/`callees` needs no schema change.

---

## Wedge 2 — Agentic Context Engineering (Policy Playbook)

**Status: usable without any external service.** The wedge no longer depends on
Packmind being up — git-native distribution is the default and is verified
against real git. Precisely:

| Component | Status |
|---|---|
| **Reflector** | ✅ **LIVE-VERIFIED** — real LLM call, 0.95 confidence, grounded in Wedge 1 temporal evidence (`valid_at`, fact count, real callers) |
| **Automatic capture** (pytest plugin) | ✅ **LIVE-VERIFIED** — real failing suite → real trace on disk |
| **Git-native distribution** | ✅ **LIVE-VERIFIED** — real repo: branch, commit, `proposed` state, HEAD restored |
| **Packmind HTTP layer** | ⚠️ socket-verified against a local fake; **never against real Packmind** |
| **Curator** — quality gate | ✅ **LIVE-VERIFIED** — real LLM judgement; rejected one candidate with a reason, passed another to submission |
| **Curator** — embedding dedup | ⚠️ still mock-only — the local endpoint serves no embeddings model (`text-embedding-3-small` → upstream 410, provider retired) |
| **PR creation via `gh`** | ❌ never executed — `gh` is not installed on this machine |

Read that table before relying on any capability below: the rows in the next
table are proven against mocks unless this one says otherwise.

Agent mistakes become coding standards. Capture (pytest plugin) → Reflector
(LLM, grounded in Wedge 1 history) → Curator (dedup + quality gate) →
distribution, via **one of two paths** selected by `PACKMIND_API_URL`:
git-native draft PRs by default, Packmind when configured.

Unlike the Wedge 1 and 3 tables above, ✅ here means "passes against a mock"
unless the **Verified against** column says otherwise. The distinction is
load-bearing — read that column, not the tick.

| Capability | Status | Evidence | Verified against |
|---|---|---|---|
| Reflector extracts a grounded rule | ✅ | Candidate carries `evidence_node`, `evidence_valid_at`, and a superseded-version count from the graph. | stub LLM + stub driver |
| Low-signal traces rejected | ✅ | LLM `null` → None; confidence 0.39 → None, 0.40 → kept. | stub LLM |
| Dedup before submission | ✅ | Cosine > 0.85 on rule embeddings → discarded, nothing created. | stub embeddings |
| Quality gate | ✅ | `passes_gate:false` → discarded with the model's reason logged. | stub LLM |
| Submission to Packmind | ✅ | Creates an unpublished standard; returns its id. | **real HTTP over a socket** (fake server) |
| Loud failure when unreachable | ✅ | `PackmindError` naming `docs/wedge2-setup.md`. Traces are never silently dropped. | fake client |
| Doctor integration | ✅ | `packmind: ok \| N rules \| last proposal <ts>` / `not configured` / `UNREACHABLE`. | real CLI, unconfigured path only |
| Evidence node resolves in the graph | ✅ | 40/40 real indexed symbols resolve through the query path. | **real index, no mock** |
| **Automatic capture on test failure** | ✅ | Real failing suite → `pending.jsonl` with `total_failed`, node ids, capped tracebacks. Clean runs write nothing. | **real pytest run** |
| **Traceback frames → graph nodes** | ✅ | Real failure → `['test_raises_inside_real_code', 'to_reflector_trace']`; graph dropped the test frame, kept the indexed function. | **real 601-node index** |
| **Git-native proposal** | ✅ | Real repo: rule file, `chronos/rule-<id>` branch, commit, `proposed` in `enforcement_rules`, HEAD restored. | **real git** |
| **Proposed rules are not enforced** | ✅ | `get_active_rules()` excludes `proposed`; `promote_to_blocking()` refuses it. | real SQLite |
| **Path routing** | ✅ | `PACKMIND_API_URL` set → packmind, unset → git-native. | real env |
| PR creation via `gh` | ❌ | Degrades to `pr_url: None`, which is tested. The `gh pr create` call itself has never run. | **nothing — `gh` not installed** |
| Reflector dispatch from a trace | ✅ | Real failing test → grounded candidate → live gate → `proposed` rule on a branch. | **real LLM, real graph, real git** |

**Suites:** `python tests/test_wedge2.py` → ALL PASS (13 checks) ·
`pytest tests/test_dual_path.py` → 11 passed ·
`pytest tests/test_pytest_plugin.py` → 16 passed ·
`python tests/test_curator_http.py` → ALL PASS (8 checks, real HTTP, no Docker).
Full suite: **42 passed**. None require an LLM, Packmind, or Docker.
**Size:** 1,191 lines across the seven Wedge 2 modules — up from 547, the cost
of a second distribution path (`rule_submission.py`, 232) and automatic capture
(`pytest_plugin.py` + `trace_processor.py`, 310). Traceback→graph resolution
added 79 lines to `trace_processor.py` on top of that.

**Round-trip:** 40/40 real indexed symbols used as evidence nodes resolve back
through the query path — a rule whose `evidence_node` the graph can't resolve
isn't grounded, it's decorated.

### Packmind API — what's actually there (researched, not assumed)

Read from the TypeScript source; the REST API is undocumented externally.
NestJS, `/api/v0`, `Authorization: Bearer <key>`. Hierarchy is
organization → space → standard → rule.

- **No MCP server exists.** The PRD asked us to prefer MCP over HTTP if available.
  It isn't — the only `*mcp*` files in the repo are Playwright demo tooling. Raw
  HTTP, isolated to `playbook.py`.
- **No rule-proposal object, and no status field.** `Standard` is
  `{id, name, slug, description, version, userId, scope, spaceId, movedTo, updatedAt}`.
  The PRD's `status: "proposed"` cannot be set.
- **Create and publish are separate calls — and that gives us the gate for free.**
  Only `POST /deployments/standards/publish` writes CLAUDE.md/`.cursor/rules`.
  Chronos never calls it, so proposed rules are inert until a human publishes
  them. That is the PRD's approval requirement in Packmind's real lifecycle
  rather than an invented field.
- **No semantic search.** `chronos_query_playbook` filters client-side. Fine at
  OSS scale; anything more is a Packmind feature request, not something to build
  around here.

**Known gap — evidence metadata has no home in Packmind's schema.** Neither
`Standard` nor `Rule` has a custom-metadata, tags, or annotations field, so
Chronos writes evidence (`evidence_node`, `evidence_valid_at`,
`evidence_commit_context`, `source`, `agent_id`) into the standard's
`description` behind a `--- chronos evidence ---` marker and parses it back out
on read. This is the one place the Packmind data model is bent rather than used
as designed. It is a real cost: a human editing that description in the UI can
corrupt the block, and `list_rules` silently ignores an unparseable one (treating
it as a hand-authored standard, which is the correct fallback but hides the
damage). The alternative was a Chronos-side rule store, which would have
duplicated the exact component we deliberately did not reimplement. Upstreaming a
metadata field to Packmind is the clean fix.

**Scope limit:** Wedge 2 is the first component that requires an LLM and network
egress. Wedges 1 and 3 remain fully offline; this is opt-in and unconfigured by
default (`chronos doctor` says `not configured`, not `ERROR`).

### Dual-path distribution — Packmind is no longer required

The evidence-metadata bend above, plus the fact that Packmind needs a running
server per org, made a single-path design a hard dependency on infrastructure a
small team may not want. There are now two paths, switched by one env var:

| | Git-native (default) | Packmind (opt-in) |
|---|---|---|
| **Trigger** | `PACKMIND_API_URL` unset | `PACKMIND_API_URL` set |
| **Store** | `.chronos/rules/<id>.yml` | Packmind standard |
| **Approval gate** | merging the draft PR | not publishing the standard |
| **Approve with** | `chronos approve-rule <id>` | publish in the Packmind UI |
| **Needs** | git (+ `gh` for the PR) | a running Packmind instance |

Both write `enforcement_rules` in `chronos.db`; the enforcement layer is
path-agnostic. Lifecycle gains a `proposed` entry state:

```
proposed → warn-only-unvalidated → warn-only-validated → blocking
```

`proposed` is **not enforced** — `get_active_rules()` excludes it and
`promote_to_blocking()` refuses it, so a rule awaiting review cannot warn on,
let alone block, a merge. Degradation is deliberate: no `gh`, no remote, or no
git at all still writes the rule file and records it as `proposed`. Every git
call is `check=False` with the return code read by hand, and the developer's
branch is restored afterwards.

### Automatic capture — and why it is not a PostToolUse hook

**Finding, verified empirically 2026-08-14: a Bash command that exits non-zero
does not fire Claude Code's PostToolUse hook.** Three failing commands were
never delivered; ten successful ones all were. The hook therefore can never see
a failing test run — the single highest-signal event this wedge exists to learn
from. The published docs were also wrong about the payload: `tool_response`
carries `{stdout, stderr, interrupted, isImage, noOutputExpected}`, with **no
`exit_code` field** and `type` null.

So capture is a **pytest plugin** (`chronos/pytest_plugin.py`), which sees every
failure by construction because pytest owns its own exit status. It writes one
session trace per failing run to `.chronos/traces/pending.jsonl`; clean runs
write nothing. Tracebacks cap at 3,000 chars and failures at 50 per session — a
2,000-test wipeout is one bug, not 2,000 lessons.

`trace_processor.py` drains that file out of band, on MCP server startup and
before `chronos enforce`. The split is the point: a test run must never pay for
an LLM round-trip (Trigger 1 measured 5,099ms inline vs 134ms without).

The Bash hook is kept as a **narrow secondary net** for commands that exit 0
while printing failure text — a linter in report mode, `pytest || true`. Its
docstring says so. It is not the primary path and cannot be.

**Gap closed 2026-08-14: `nodes_touched` is now populated on pytest traces.**
It was empty, so every auto-captured lesson reached the Reflector ungrounded.
The fix does *not* map the test id — `tests/t.py::test_a` names the test, not
the code under test, and mapping it would invent a relationship rather than
observe one. Instead `candidate_symbols()` reads function names out of the
traceback (all three frame shapes pytest emits: `def f(`, `path:n: in f`, and
the stdlib `File …, in f`), and `resolve_nodes()` keeps only the ones the graph
confirms, capped at 10.

Verified against a real 601-node index of this repo: a real failing test
produced `['test_raises_inside_real_code', 'to_reflector_trace']`, and the graph
dropped the test frame and kept the indexed function. When nothing resolves the
list stays empty and `reflector.ground()` still labels the lesson ungrounded —
the honest fallback is preserved, it is just no longer the only outcome.
Resolution runs in `_dispatch_safe` (off the test-run path, driver closed before
the LLM call) and is best-effort: a resolution failure logs and reflects
ungrounded rather than dropping the trace.

### Live end-to-end run — capture → reflect → curate → distribute (2026-08-14)

The chain ran with **no mocks at any stage**, against an OpenAI-compatible
endpoint. This is the first time an auto-captured trace has become a proposed
rule without a human in the middle.

1. **Capture** — a real failing test wrote `pending.jsonl` (pytest plugin).
2. **Grounding** — traceback frames resolved to the real graph node
   `to_reflector_trace`: `last_changed 2026-08-14T00:53:32+00:00`, 3 facts,
   0 superseded, callers `[test_to_reflector_trace_shape, _dispatch_safe]`.
   This is the #2 mapping doing its job — without it the Reflector would have
   seen no nodes at all.
3. **Reflect** — three separate models each returned a grounded rule carrying
   that evidence. Confidence 0.86 / 0.85 / 0.90, and 0.97 on a later run.
4. **Quality gate** — ran live and **rejected** the first candidate:
   *"overly strict and would generate false positives for legitimate
   optional-dict patterns."* A real judgement with a reason, not a rubber stamp.
   A second, narrower candidate passed.
5. **Distribute** — git-native path wrote `.chronos/rules/<id>.yml`, created
   branch `chronos/rule-<id>`, recorded the rule `proposed`, and restored HEAD
   to `master` with uncommitted work intact. `gh` absent → `pr_url: None`, the
   documented degradation.
6. **Gate held** — the live-generated rule was excluded from `get_active_rules()`
   and `promote_to_blocking()` refused it (*"still proposed — run approve-rule
   first"*).

All run artifacts (rule file, branch, `enforcement_rules` row) were removed
afterwards.

**What this run did NOT prove:**

- **Embedding dedup is still mock-only.** The endpoint returned upstream 410 for
  `text-embedding-3-small` (provider retired). With an empty rule store
  `_duplicate_of` short-circuits before embedding, so the cosine > 0.85 path
  never executed. This is the last mock-only piece of Wedge 2's LLM work.
- **Model availability was flaky**, not a code fault: two models returned
  `RateLimitError: All models exhausted` and the same trace succeeded on others.
  Worth noting because a Reflector failure in production is indistinguishable
  from a low-signal trace unless the logs are read — the dispatch path swallows
  it by contract.
- Nothing here touches Packmind; the git-native path was the one exercised.

### Live verification: Reflector passed, Packmind still blocked

**The Reflector ran live on 2026-08-13** against an OpenAI-compatible endpoint
(any litellm-supported provider works; nothing in Chronos depends on which).
Grounded on `getActor` from a real 1,502-node graph, it returned:

> *"IF a function makes an HTTP request (e.g., getActor) THEN it must call the
> shared http client wrapper instead of invoking fetch() directly, so that auth
> headers and retry logic are preserved."* — confidence **0.95**

with `evidence_valid_at: 2026-07-20T15:50:43+00:00` and
`evidence_commit_context: "33 facts, 0 superseded; callers=[create, update, remove…]"`.

That last part is the whole premise of the wedge: the model reflected on **real
temporal facts from the graph**, not a decontextualized trace. It had only ever
been exercised against stubs before. Also confirmed live: the `IF … THEN …`
format instruction, the 0.4 confidence floor, and JSON parsing.

The Curator then failed exactly as designed —
`PackmindError: Packmind not reachable — run docs/wedge2-setup.md` — a loud,
clean exit rather than a silent discard of the trace.

**Still blocked: Packmind itself.** `docker` is not installed on this machine —
no `Program Files\Docker`, no service, no PATH entry, and WSL has zero
distributions. `%LOCALAPPDATA%\Docker` holds only orphaned logs from an install
that never finished (`No installation found`, then an uncompleted UAC relaunch).
Packmind ships only as a Compose stack, so the HTTP layer stays unverified.

**What this leaves unproven — the honest risk list:**

- Every HTTP request in `playbook.py` is built from *reading* Packmind's
  TypeScript, never from a response. Paths, payload shapes, and status codes are
  inferred.
- **Highest-risk single call: `GET /auth/me`.** `_scope()` expects
  `{organization: {id}, spaces: [{id}]}`. If the real shape nests differently,
  `Packmind()` fails at construction and *nothing* in Wedge 2 works. Setting
  `PACKMIND_ORG_ID`/`PACKMIND_SPACE_ID` explicitly bypasses this path.
- The evidence-in-`description` round-trip is verified only as a string operation,
  not through a real create-then-read cycle.
- `create_standard` reads the id as `r["id"]` with a fallback to
  `r["standard"]["id"]` because the controller's return type and the use-case
  response type disagree in the source. One live call settles which is right.
- **The Curator's quality gate is now live-verified** (2026-08-14) — it rejected
  one candidate with a reason and passed another. **Its embedding dedup is not**:
  no embeddings model was reachable, so cosine > 0.85 has still never run
  against real vectors.

Clearing this needs Docker Desktop (admin/UAC); an LLM key is no longer a
blocker. The procedure is
`docs/wedge2-setup.md`.

---

## Wedge 4 — Executable Policy Governance (CI Enforcement)

Closes the loop: a Wedge 2 playbook rule becomes an ast-grep pattern, OPA decides
block vs warn, and blocks are stamped into Wedge 3's ledger. Unlike Wedge 2, both
external tools were **installed and exercised on this machine** before the code
was written, and the suite's final test runs against them for real.

**Both blockers from the validation run are now fixed** (commit `8b22cd4`):

| Blocker | Was | Now |
|---|---|---|
| **Trigger 1 latency** | Reflector called synchronously inside `enforce()` — **5,099 ms** per blocking verdict, a 38x tax on exactly the CI runs that block most | Dispatched to a daemon thread. **28 ms** enforce path, measured with a 3 s Reflector running behind it. `drain(timeout)` added so short-lived CLI processes do not exit before the lesson is captured |
| **Language field** | Absent from node dicts entirely (`kind`/`name`/`path`/`qname` only), so per-language rule scoping filtered on nothing and every rule applied to every file | Derived from file extension via `os.path.splitext`. Verified on Setu: **683 typescript, 176 javascript**, and **643 Folder/Module/Project nodes correctly `unknown`** (never `None`). Also carried into the graph — `sync.py`'s `attributes` dict is explicit, so the field had to be added there or it was silently dropped at the boundary |

The latency fix is pinned by a regression test that fails if the Reflector ever
moves back onto the enforcement path.

| Capability | Status | Evidence | Verified against |
|---|---|---|---|
| Plain-English rule → ast-grep YAML | ✅ | Fenced YAML parsed, written to `.chronos/rules/<id>.yml`. | stub LLM |
| NOT_AUTOMATABLE path | ✅ | "use clear variable names" → no pattern, reason retained. | stub LLM |
| CHECK A rejects invalid YAML | ✅ | ast-grep exit 8 → `syntax_valid: false`, CHECK B skipped. | **real exit codes** |
| CHECK B rejects a rule that misses its own example | ✅ | 0 matches on the positive snippet → `passed: false`. | stub scan |
| False-positive risk flagged | ✅ | Rule firing on the negative snippet → flagged, still stored. | stub scan |
| Block requires promotion **and** graph confirmation | ✅ | blocking + superseded → `block`; blocking + not superseded → `warn`. | **real OPA** |
| Warn-only never blocks | ✅ | Graph confirms deprecation, verdict still `warn`. | real OPA |
| Blocks stamped into Wedge 3 | ✅ | `provenance_events` row: action `blocked_by_ci`, agent, session, rule id. | real SQLite |
| Warns are never stamped | ✅ | Ledger count unchanged after a warn verdict. | real SQLite |
| Promotion gate | ✅ | Unvalidated rule → refused; validated → `blocking`. | real SQLite |
| No silent demotion | ✅ | Regenerating a promoted rule keeps it `blocking`. | real SQLite |
| Audit report | ✅ | 4 blocks, top rule and top node ranked from the ledger. | real SQLite |
| End-to-end on real tools | ✅ | Real ast-grep matched `createClient({url: 'x'})`, real OPA returned `warn`. | **real ast-grep 0.45.1 + real OPA 1.19.0** |

**Suite:** `python tests/test_wedge4.py` → ALL PASS (16 checks). Mocks the LLM
throughout; the final test uses the real binaries and skips cleanly without them.
**Size:** 463 lines of code across the five modules (see budget note below).

### Tool research — what the PRD assumed vs what is true

Both tools were installed and probed before any code was written. Two PRD
assumptions were wrong, and both would have produced silently broken enforcement:

- **The PRD's Rego does not parse.** It is Rego v0 (`enforce := result { ... }`);
  OPA ≥ 1.0 requires v1 and rejects it with `rego_parse_error: "if" keyword is
  required before rule body`. `chronos/policies/enforce.rego` is the v1 form with
  identical logic, verified across all three branches.
- **ast-grep's exit code does not indicate matches.** It is 0 whether or not
  anything matched; 8 means the rule could not be parsed, 6 means the file is
  missing. So "did anything match" must come from the JSON array length and
  "is this rule valid" from the exit code. Conflating them — the natural reading
  of the PRD — would make an unparseable rule look like a clean pass, silently
  disabling the check.
- **`sg` is deprecated** in 0.45.1 (prints a warning and defers to `ast-grep`).
  We invoke `ast-grep`; `CHRONOS_ASTGREP` overrides.

Full notes, including the JSON match shape, are at the top of `enforcer.py`.

### Scope limits, stated plainly

- **`total_checks`/`warns`/`passes` in `chronos_rule_report` return `null`.** Only
  blocks are persisted (into the ledger); warn and pass verdicts are returned live
  by `chronos_enforce` and never written. The PRD asked for all four counts —
  producing the other three would mean a write on every clean CI run, so the
  report returns `null` with a note rather than a fabricated number.
- **Symbol extraction from a match is a heuristic.** `_identifier` prefers a
  captured metavariable and otherwise takes the head of the matched snippet. A
  wrong guess costs a graph miss, which degrades to `warn` — it cannot cause a
  spurious block.
- **Budget:** 463 lines of code against the 500 limit, but 715 lines on disk. The
  difference is 131 docstring lines, ~65 of which are the Step-0 research block
  the PRD required in `enforcer.py`. Keeping that block was chosen over hitting
  the raw line count, since it is where the two findings above are recorded.
- **No live CI run — but the workflow is now installed and its blockers fixed.**
  It moved from `docs/wedge4-ci.yml` to `.github/workflows/chronos-enforce.yml`,
  where Actions will actually pick it up. Reviewing it against the CLI before
  the first run found two faults that would have failed or silently no-opped it:
  - **`--diff origin/${{ github.base_ref }}` does not resolve.** `actions/checkout`
    leaves the base branch unfetched on a `pull_request`, so `git diff` exits
    non-zero and `enforce` dies on `sys.exit`. Fixed with an explicit
    `git fetch origin <base_ref>` step feeding `FETCH_HEAD`, falling back to
    `HEAD~1` on `push`.
  - **`--lang typescript` was hardcoded**, which forces one language onto every
    file in the diff. Dropped — `enforce` already infers per-file language via
    `indexer.node_language`, which is the fix that shipped with the language
    field.

  Also: `--fail-on-block` is now omitted deliberately (advisory first — with no
  graph in CI every verdict degrades to `warn` anyway), a `pytest tests/ -q` step
  was added so the run doubles as the Linux check for gap 1, and a comment
  records that the indexer submodule is intentionally not fetched (enforcement
  reads a graph, it does not index).

  **Still unrun, and this repo cannot run it: there is no git remote.** Pushing
  to a GitHub remote is the remaining step, and it is infrastructure, not code.
  `enforce` itself was executed locally against real HEAD~1 and completed
  cleanly (`Checked 0 files - 0 block, 0 warn, 0 ok`).

---

## Unification

Packaging change, not a wedge change. No wedge logic was modified — the diff is
aggregation, wiring, and naming.

**What changed**

| Before | After | Why |
|---|---|---|
| 4 MCP servers | 1 (`chronos-mcp`, 19 tools) | The wedges were never independent products. Shipping four servers exposed our internal decomposition as the partner's integration problem: four commands, four env blocks, four things to notice were missing. |
| `ledger.db` (SQLite) | `chronos.db` (SQLite) | One path to name, one file to back up. The name no longer implies it belongs to Wedge 3. |
| 0 cross-wedge triggers | 3 | The wedges fed each other only when an agent chose to call the next one by hand. |
| `pytest tests/ -q` collected 0 tests | collects 5 | It reported success vacuously — a green bar that proved nothing. |

**Correction to the stated premise.** The task described three SQLite files to
consolidate. An audit of every `sqlite3.connect()` found **one**: `rule_store.py`
already called `ledger.connect()`, so `intent_locks`, `provenance_events` and
`enforcement_rules` were already colocated. The third connect is `upstream.py`
opening codebase-memory-mcp's index read-only — a foreign file we consume, not
Chronos state, and deliberately not consolidated. What actually shipped is the
rename, a single connection manager (`db.py`) so PRAGMAs cannot drift between
callers, and a migration for existing installs.

**Env var deviation.** The spec put the SQLite path in `CHRONOS_DB`. That name
was already taken by `store.py` for the **graph** path, so reusing it would have
silently pointed Kuzu at a `.db` file. The SQLite path is `CHRONOS_SQLITE`;
`CHRONOS_LEDGER` is still honoured so existing installs need no edit.

**Naming deviation.** The spec imported Wedge 1's tools from `wedge1_mcp` as
`chronos_node_history`/`chronos_sync`. No such module or tools existed — Wedge 1
lived in `server.py` as `as_of_callers`/`as_of_callees`/`as_of_impact`/
`what_changed`/`index_health`. That module is now `wedge1_mcp.py` and `server.py`
is the aggregator, but the **tool names are unchanged**: renaming them would
break every existing agent config for a cosmetic gain.

**Triggers**

| Event | Source → Target | Action | On failure |
|---|---|---|---|
| CI block | 4 → 2 | Auto-reflect the block into a candidate rule, then curate | Logged; **verdict unaffected** |
| Node deprecated | 1 → 4 | Warn when no active rule covers the node | Logged only |
| Lock conflict | 3 → 2 | Coordination-lesson hint | Logged only |

Trigger 1 is automatic because a CI block is the highest-signal event in the
system: a labelled failure with an action, a reason and a node, already in the
Reflector's trace shape. The manual path needs an agent to notice it failed and
choose to report it — and the agents worth learning from are the ones that
confidently did the wrong thing and won't self-report.

Trigger 2 warns rather than acts: generating a rule costs an LLM call and needs
human approval. It exists because the deprecated-but-unenforced node is the
system's quietest failure — agents keep using a superseded symbol and Wedge 4
passes, since a rule that doesn't exist cannot fire.

Trigger 3 does not reflect at all. Conflicts are weak signal; two agents wanting
the same node is often legitimate concurrency. Auto-reflecting each one would
flood the playbook and train the Curator's dedup against us.

Backward compatibility: the four old entry points remain as aliases that start
the unified server and print a deprecation notice to stderr (never stdout, which
carries the MCP protocol).

**Suite:** `python tests/test_unification.py` → ALL PASS (15 checks), including
trigger isolation (a throwing Reflector leaves the block verdict and its
provenance stamp intact), the kill switch, thread-local connection reuse, and
legacy migration. `pytest tests/ -q` → 5 passed.

**Still pending** (unchanged by this work): live Packmind test, live CI run,
Linux `_run_posix` build, `chronos gc` on a fresh repo.

---

## Verified against real repos

Not fixtures — third-party projects indexed with the locally-built indexer.

| Repo | Nodes | Edges | Index | Facts | Top query |
|---|---|---|---|---|---|
| Setu | 1,502 | 322 | 16.6s | 297 | `getActor` → 33 callers |
| MediAssist | 2,415 | 398 | 16.6s | 376 | `append` → 25 callers |
| Opencode | 9,907 | 3,034 | 16.5s | 2,601 | `getDb` → **68 callers** |
| ortho | 10,889 | 5,678 | 17.7s | 5,609 | `str` → 353 callers |

Zero indexing failures, zero empty results. Drishti was excluded during triage —
121 files, all `.docx`/config, no source.

Also verified: Shiplog (TS/JS, `createClient` → 13 callers) and Chronos itself
(live rename correct before/after).

**Fresh-clone build:** `python -m chronos.build_cbm` → 5m03s, 295 MB, exit 0,
then indexed a repo it had never seen.

---

## Performance

| Metric | Value | Note |
|---|---|---|
| Sync throughput | **~2,100 writes/sec** | Was 30/s. Cause was serializing 1024-float embedding vectors, found by measurement — not transaction overhead, which was my first (wrong) hypothesis. |
| 5k nodes + 20k edges | **12s** | Previously exceeded a 10-minute timeout. |
| No-op re-sync | 2.1s | Content-hash short-circuit. |
| Query p50 | 58ms | Well inside the sub-second p95 target. |
| Index (350-file repo) | 12–14s | Upstream's C indexer. |
| CLI `enforce`, cold | 5.5–8.7s | Fresh interpreter. Dominated by imports, not work. |
| CLI `enforce`, with daemon | **167–206ms** | ~30x. See below. |

P0-3's 5-minute SLA holds with large margin at these sizes. **Not yet load-tested
at multi-million LOC**, which the v1 PRD explicitly flags as an open question.

### The daemon — why CLI latency was import cost, not driver cost

Every CLI invocation is a fresh interpreter, and the pre-commit hook is the
worst case: a developer pays it on every commit. Profiled before building
anything:

```
bare interpreter          70 ms
json + socket            108 ms
import chronos.cli      5039 ms   <- what every CLI call paid
  of which graphiti_core 3900 ms  (transitively imports openai + neo4j)
  opening Kuzu on top      ~0 ms
```

The driver was **not** the bottleneck; the import chain was. That finding
shaped the design: a daemon that only kept the driver warm would have saved
nothing, because the client still had to import `chronos.cli` to ask. So
`chronos/__main__.py` checks for the daemon *before* that import, using a
stdlib-only client (128ms), and only falls through to the full CLI when the
daemon cannot serve the request.

| | Cold | With daemon |
|---|---|---|
| `enforce --file` | 5,466 / 8,284 / 8,736 ms | **206 / 196 / 167 ms** |

Startup is ~7s, paid once. Output is byte-identical between the two paths —
pinned by a test, because two renderers that drift would make the gate's answer
depend on how it was invoked.

**Operational consequence, and the reason this is not free:** Kuzu takes an
**exclusive file lock** on the graph, so exactly one process may hold it. While
the daemon runs, a direct `index`/`sync`/`health`/`gc` cannot open the store.
That is a property of the embedded store, not something the daemon can avoid,
so it is surfaced rather than hidden: `open_driver()` raises `GraphLocked` with
the remedy (`daemon stop`), `index` claims the graph *before* the ~15s indexer
runs rather than discovering the conflict after, and `doctor` degrades to
`chronos : LOCKED ...` and still prints every other line — a diagnostic that
dies on the condition being diagnosed is useless.

Disable with `CHRONOS_DAEMON=0` (honoured even when a daemon is running).

---

## Gaps

**1. Non-Windows build unverified.** `_run_posix` is the simpler branch (plain
`make`, no MSYS2 path translation or `TMPDIR` workaround) but has never executed
here. One Linux CI run settles it.

**1b. Adjacent-node conflict detection (Wedge 3).** The platform PRD's Wedge 3 P0
asks for the graph to catch structurally-adjacent edits. Locks currently cover
exactly the nodes an agent names. Additive; no schema change needed.

**2. Scale.** Largest real index is 1502 nodes. Behavior on a multi-million-LOC
monorepo is unmeasured, and Kuzu is marked deprecated upstream — isolated to
`store.py`, and `CHRONOS_DB_URI` already routes to Neo4j when a partner outgrows it.

---

## Decisions

**D-1. Two MCP servers, not one proxy.** *(Resolved 2026-08-13. Supersedes P0-4's
"proxied from Codebase-Memory MCP" scope and answers the PRD's open product
question: "mirror the 14–15 tools 1:1, or a smaller opinionated set?")*

The PRD described proxying upstream's current-state query tools through Chronos's
MCP server so agents talk to one server. This was deliberately not built. Proxying
15 upstream tools Chronos adds nothing to is surface area without value — it would
make Chronos a pass-through for queries it does not own. The correct architecture is
two MCP servers coexisting: **Chronos for temporal queries, upstream for
current-state queries.** Agents are already equipped to talk to multiple MCP servers.

Consequences, stated plainly so this stays reviewable:

- Chronos exposes 5 tools (4 temporal + health), all of which answer questions no
  other server in the stack can. Nothing is a passthrough.
- Operators register two servers instead of one. This is configuration, not
  integration work — see README for the block.
- The boundary matches ownership: upstream owns current structure, Chronos owns
  history. A proxy would have blurred it, and any upstream tool change would have
  become a Chronos maintenance burden.
- Reversible. If a partner requires a single endpoint, proxying upstream's tools is
  additive and touches only `server.py`.

**No LLM in the write path.** Graphiti's `add_episode`/`add_triplet` run LLM entity
extraction and embedding dedup per fact. AST facts are already structured, and fuzzy
dedup would merge distinct same-named functions. Chronos writes `EntityNode`/
`EntityEdge` via their documented `.save()`, which sets `valid_at`/`invalid_at`
natively. This is what makes "no external services" literally true.

**Identity = upstream's `qualified_name`.** Testing on a real repo (not fixtures)
found `path::name::kind` collided **6 times in 379 nodes** — nested closures, and
folders sharing a basename with files. `qualified_name` collided 0 times and is
`UNIQUE` per project upstream. This mattered: colliding identity merges distinct
functions into one temporal history and yields confidently wrong as-of answers.

**Both input paths must agree on identity.** The above fix initially reached only
the sync path: `indexer.py` dropped `qname` when building node dicts, so everything
written by `chronos index` silently fell back to the colliding scheme, and the two
paths disagreed with no error anywhere. Caught by round-tripping real indexed nodes
back through the query path — **0/300 found**, now 300/300. A permanent test
(`roundtrip()`) indexes a real repo and asserts 100% lookup, because this class of
bug is invisible to any test that exercises one path at a time.

**gc requires a superseded fact, not merely no current one.** A node with no facts
at all is a valid symbol — Chronos syncs every upstream node but only
`TEMPORAL_EDGE_TYPES` edges, so most nodes have no edges. The literal "no current
facts" rule flagged **18,248 nodes on Opencode where only 801 were real orphans**.
Deleting the difference would have destroyed most of the graph.

**Vendored, not copied.** The cross-file type resolution that distinguishes
`client.send()` from `server.send()` spans `src/pipeline/` + `internal/cbm/`
(~292k lines of C). Copying a subset yields a parser without the resolution;
copying all of it is a fork. Built from a pinned submodule instead, so upstream
fixes arrive as a `git pull`.

---

## Next

- **Blocking on nobody:** Linux CI build; load test on a large monorepo;
  adjacent-node conflict expansion for Wedge 3.
- **Wedge 2 — no longer blocking anything.** Git-native distribution is the
  default and needs no external service, so the wedge is demoable end-to-end
  today. Three gaps remain, none blocking:
  1. **A live Packmind run** — still blocked on a container runtime, not on
     code. The HTTP client is now socket-verified against a local fake
     (`tests/fake_packmind.py`, `chronos doctor --fake-packmind`), which proves
     our client is internally consistent, **not** that the real API agrees with
     our reading of its TypeScript. Podman or WSL2 is the cheapest path.
  2. **`gh pr create` has never executed** — `gh` is not installed here. The
     no-`gh` degradation is tested; the PR path itself is not.
  3. **CLOSED 2026-08-14 — a trace has now produced a rule end-to-end**, live,
     with nothing mocked. See "Live end-to-end run" below. The one piece still
     mock-only is the Curator's **embedding dedup**: the endpoint used serves no
     embeddings model, so `_duplicate_of` was skipped (empty rule store) rather
     than exercised. Cosine > 0.85 remains unproven against real vectors.
- **Upstream to Packmind:** request a metadata field on `Standard`, so evidence
  stops living inside `description`. Their response time is itself the signal on
  whether Packmind is a safe long-term dependency.
- **Wedge 4:** the workflow is now installed at
  `.github/workflows/chronos-enforce.yml` with both of its pre-run faults fixed
  (unfetched base ref, hardcoded `--lang`), but it has still never executed —
  **this repo has no git remote**, so the first run is blocked on infrastructure.
  Enforcement also stays toothless until a graph is restored in CI (every verdict
  degrades to `warn`).
- **Per platform PRD sequencing:** all four wedges are built. Wedges 1, 3 and 4
  are verified against real tools and repos; Wedge 2 is verified against real
  git, real pytest and a real socket, but not against a real Packmind.
  **Deviation from PRD P0-1** ("deploy Packmind OSS as the playbook store"):
  Packmind is now opt-in rather than required, because a per-org server is
  friction a small design partner may refuse. The Packmind path is fully built
  and remains the org-scale answer.
  Wedge 4 uses ast-grep (MIT) and OPA (Apache 2.0) via subprocess — Opengrep
  (LGPL-2.1) is used nowhere. Wedge 2's Reflector/Curator was built in-house because
  the ACE framework is FSL-licensed; Packmind OSS (Apache 2.0) is used unmodified
  as the store. Wedge 3 was
  built in-house on SQLite rather than by extending Forge Orchestrator — the PRD's
  spike question ("can its file locking extend to AST-node granularity without a
  fork?") was bypassed: node-level locking keyed on Wedge 1 identities is ~260
  lines and needs no external coordination substrate.

---

## Maintenance

`chronos gc` removes nodes whose facts have all been superseded — the residue of
identity migrations. Dry-run by default; `--execute` to delete. `chronos doctor`
warns above 10% orphans and prints the command.

Verified lossless on a real repo: a forced identity migration on Setu produced 170
orphans; gc deleted exactly those, leaving `facts_current` at 297 and
`callers(getActor)` at 33.

**Known caveat:** an early, buggy version of gc was run against the scratch graph
used during repo testing and stripped `RELATES_TO` relationships from live fact
nodes there. That store is disposable and was rebuilt. The committed gc does not
have this defect — confirmed by rebuilding from scratch, forcing a migration, and
re-running — and a regression test now pins the invariant (live facts and
fact-less symbols both survive).
