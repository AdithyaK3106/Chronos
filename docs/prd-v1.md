# PRD: Chronos v1 — Bi-Temporal AST Knowledge Graph

**Status:** Draft for review
**Owner:** [Product/Founder]
**Target release:** [TBD — see Timeline Considerations]
**Wedge:** 1 of 4 (Bi-Temporal AST Hybridization) — Wedges 2–4 (ACE playbook, intent ledger, CI enforcement) are explicitly deferred to v2+
**Build approach:** Integration/orchestration on top of existing open-source frameworks — **not** a from-scratch build. Codebase-Memory MCP (AST parsing + SQLite structural graph) and Graphiti (bi-temporal graph engine) are treated as hard dependencies. The actual product surface is the mapping/sync layer between them plus a unified MCP interface — see Requirements.

---

## Problem Statement

Platform and DevEx teams at mid-size engineering orgs (50–500 engineers, dozens of repos) are rolling out AI coding agents (Claude Code, Cursor, Copilot, Devin) faster than they can keep those agents accurate about the current state of the codebase. Agents rely on either (a) grep-and-read exploration, which burns enormous token budgets re-reading the same files every session, or (b) RAG/vector retrieval over docs and code, which cannot distinguish a deprecated pattern from a current one because superseded and current implementations are often semantically near-identical in embedding space. The result: agents confidently reintroduce deprecated APIs, contradict recent refactors, and generate PRs that are locally correct but globally inconsistent — forcing platform teams into constant manual review and eroding trust in agent-generated code. As agent fleets scale from a handful of pilots to dozens of concurrent sessions across a repo, this failure mode compounds and becomes the primary blocker to expanding agent usage.

## Goals

1. Reduce agent context-retrieval token spend for structural code questions ("what calls this function," "what changed here") by ≥90% versus grep/file-read baselines, measured across pilot repos.
2. Enable agents to correctly answer "as of" structural questions (e.g., "was this function calling X before last week's refactor?") with ≥90% accuracy on a held-out eval set, where current-state-only tools score at or near 0%.
3. Get a mid-size eng org's platform team to a working, self-hosted deployment indexing their primary monorepo (or top 3 repos) within a single onboarding session (<1 day), no dedicated infra hire required.
4. Achieve measurable reduction in deprecated-pattern reintroduction in agent-authored PRs at pilot accounts — target 50% reduction in "reverted for using old pattern" review comments within 60 days of rollout.
5. Land 5 paid design partners (mid-size platform/DevEx teams) within the first two quarters post-launch, each indexing at least one production repo >100k LOC.

## Non-Goals

- **ACE-based self-improving policy playbook (Wedge 2).** This is a distinct product surface (organizational rule learning) that depends on having the bi-temporal graph live first. Out of scope for v1; tracked as v2.
- **Cross-agent intent/provenance arbitration (Wedge 3).** Coordinating concurrent multi-agent writes requires the graph to already be trusted and adopted. Premature before v1 proves the core memory layer works.
- **CI/CD enforcement and PR blocking (Wedge 4).** Enforcement is a governance/trust step that follows adoption, not precedes it — blocking merges on day one of a new tool would kill pilot goodwill. Deferred to v2.
- **Non-code organizational memory (Slack, Jira, meeting notes).** v1 is scoped strictly to the codebase graph. Ingesting organizational chatter is a large surface area (ontology design, PII handling) that isn't required to prove the core bi-temporal AST thesis.
- **Reimplementing AST parsing or the bi-temporal engine from scratch.** Codebase-Memory MCP (parsing, structural graph) and Graphiti (bi-temporal primitives, valid/transaction time tracking) are adopted as-is wherever their existing APIs cover the need. Engineering effort goes into the sync/mapping layer between them, not into rebuilding either. Any gap that seems to require reimplementing core parsing or temporal logic should be treated as a signal to re-scope, not a green light to fork.
- **Hosted/managed SaaS deployment.** v1 ships self-hosted (matching Codebase-Memory MCP's zero-dependency binary model) to minimize security review friction with platform teams; managed hosting is a v2+ commercial consideration.
- **Full 158-language parity from day one.** v1 targets the languages the design partners actually run in production (expect Python, TypeScript/JS, Go, Java as the core set) rather than exhaustive coverage.

## User Stories

**Primary persona: Platform/DevEx engineer** (owns the internal tooling that AI agents plug into)
- As a platform engineer, I want to point Chronos at our monorepo and have it build a queryable structural graph without manual configuration, so that I can stand up a pilot in under a day.
- As a platform engineer, I want the graph to update incrementally on every commit rather than requiring a full re-index, so that the graph never falls more than one commit behind main.
- As a platform engineer, I want to query "what did the call graph for this service look like before commit X" via an MCP tool, so that I can debug agent-introduced regressions without manually checking out old commits.
- As a platform engineer, I want visibility into index health (freshness, coverage, failed parses) so that I can trust the graph before rolling it out org-wide.
- As a platform engineer, I want to configure which repos and branches are indexed, so that I control blast radius during the pilot phase.

**Secondary persona: AI coding agent (via MCP client — Claude Code, Cursor, etc.)**
- As an agent, I want to query the current call graph for a function so that I don't have to grep and read multiple files to understand its usages.
- As an agent, I want to query the historical state of a structural node as of a given date or commit so that I don't hallucinate a deprecated pattern as current.
- As an agent, I want a clear signal when a queried node has no valid data at the requested time (rather than a silent fallback to current state) so that I don't over-trust an empty result.

**Edge cases**
- As a platform engineer, I want a clear error state when a repo fails to parse (unsupported syntax, corrupted files) rather than a silent partial index, so that I know which parts of the graph are trustworthy.
- As an agent, I want temporal queries against a point in time before the graph existed to return an explicit "no data" response, not a fabricated answer.

## Requirements

### Must-Have (P0)

**P0-1: Adopt Codebase-Memory MCP as the parsing/indexing engine**
Run Codebase-Memory MCP unmodified (or with minimal patches upstreamed where possible) as the source of structural nodes (functions, classes, call edges, HTTP routes) for the pilot's core language set. No new parser is written.
- Given a supported repo, when Codebase-Memory MCP indexes it, then Chronos consumes its output (SQLite graph / event stream) as the sole source of structural truth — Chronos does not re-parse source itself.
- [ ] Initial full index completes and Chronos successfully ingests per-file parse success/failure counts from Codebase-Memory MCP's own reporting.
- [ ] Any patch required to Codebase-Memory MCP is documented and proposed upstream rather than maintained as a silent divergent fork.

**P0-2: Sync layer — map Codebase-Memory MCP's structural graph into Graphiti's bi-temporal model**
This is the core engineering deliverable of v1: a translation layer that takes structural add/change/remove events from Codebase-Memory MCP's SQLite graph and writes them into Graphiti as bi-temporal facts (`valid_at`, `invalid_at`, `created_at`, `expired_at`), using Graphiti's existing episode/fact ingestion APIs rather than a custom store.
- Given a function signature changes in a new commit, when the sync layer processes the resulting Codebase-Memory MCP diff, then it writes an `invalid_at` on the old Graphiti fact and a new fact with `valid_at` = commit time, via Graphiti's native supersession mechanism.
- [ ] Superseded nodes remain queryable via Graphiti's existing "as of" query support.
- [ ] Current-state queries never return an invalidated edge by default.
- [ ] Sync layer has no independent source of truth — Codebase-Memory MCP owns current structure, Graphiti owns temporal history; the sync layer is stateless glue, re-derivable from both systems if lost.

**P0-3: Incremental re-indexing on commit**
Rely on Codebase-Memory MCP's existing incremental/watch-mode indexing (file-watcher / commit hook) to detect changes; the sync layer consumes its incremental diffs to update Graphiti, rather than Chronos building its own change-detection.
- Given a commit lands on the tracked branch, when Codebase-Memory MCP re-indexes the changed files, then the sync layer propagates only the delta into Graphiti within a defined SLA (target: under 5 minutes for a typical commit).
- [ ] Incremental updates never require a full re-sync of the Graphiti store.

**P0-4: Unified MCP query interface**
Expose a single MCP surface to agents that routes current-state structural queries to Codebase-Memory MCP's existing tools and "as-of"/historical queries to Graphiti's query API, so the agent doesn't need to know which backend answers which question.
- Given an agent issues an "as of [date/commit]" query, when Graphiti has data for that period, then it returns only facts valid at that time.
- Given no data exists for the requested time window, when queried, then the tool returns an explicit empty/no-data result rather than falling back to current state.
- [ ] At minimum: current call graph and current impact analysis (proxied from Codebase-Memory MCP) plus as-of call graph and as-of impact analysis (served from Graphiti) are exposed as distinct tools behind one MCP server.

**P0-5: Self-hosted deployment bundling both dependencies**
Package Codebase-Memory MCP's binary and a Graphiti instance (plus the sync layer) as a single deployable unit (e.g., one container/compose file) with no required external services, so platform teams can pass security review without new infra approvals or having to separately stand up two open-source projects themselves.
- [ ] Runs fully within the customer's environment; no code or graph data leaves the customer's network by default.
- [ ] Standing up both upstream dependencies plus the sync layer takes one install step, not two separate OSS setup processes.

**P0-6: Index health dashboard/CLI**
Basic visibility into freshness (last successful index time), coverage (% of files successfully parsed), and errors.
- [ ] Platform engineer can answer "is this graph safe to rely on right now" in under 30 seconds.

### Nice-to-Have (P1)

**P1-1: Multi-repo cross-service linking**
Extend HTTP route/call linking across repo boundaries (as Codebase-Memory MCP already supports) so agents can trace calls across microservices.

**P1-2: Slack/notification on index failure**
Alert platform engineers when incremental indexing fails or falls behind SLA, rather than requiring manual dashboard checks.

**P1-3: Query result caching**
Cache frequent "as of" queries to reduce repeated graph traversal cost for common agent query patterns.

**P1-4: Branch-aware indexing**
Support indexing feature branches (not just main) so agents working on in-flight branches get accurate structural context.

### Future Considerations (P2)

**P2-1: ACE playbook integration hooks** — reserve schema/API surface so the graph can later feed the Reflector/Curator loop (Wedge 2) without a breaking migration.

**P2-2: Intent registration API** — reserve an extension point for agents to register write-intent against graph nodes (Wedge 3), even though arbitration logic itself is out of scope for v1.

**P2-3: Non-code memory sources** — design the ontology so Slack/Jira ingestion (organizational memory) could plug in later without re-architecting the bi-temporal core.

## Success Metrics

**Leading indicators (days–weeks)**
- Time-to-first-successful-index for a new pilot repo (target: <1 day, stretch: <2 hours).
- % of agent structural queries served by Chronos vs. falling back to grep/file-read, measured via MCP tool call logs (target: >70% of eligible queries within 30 days of rollout).
- Index freshness: % of time the graph is within 1 commit of HEAD (target: >95%).
- Query latency for as-of and current-state queries (target: sub-second p95).

**Lagging indicators (weeks–months)**
- Reduction in "reverted for deprecated pattern" review comments on agent-authored PRs at pilot accounts (target: 50% reduction within 60 days).
- Token spend reduction for agent structural exploration, measured via before/after comparison at pilot accounts (target: ≥90% reduction, tracked against the published Codebase-Memory MCP baseline).
- Design partner conversion: pilots that convert to paid contracts (target: 5 of 8–10 initial pilots within two quarters).
- Platform team NPS/qualitative feedback on trust in agent-generated code post-rollout.

## Open Questions

- **[Engineering]** Confirmed: integrate Codebase-Memory MCP and Graphiti as external dependencies via a thin sync/orchestration layer (P0-2), not a fork. Open sub-question: do we consume Codebase-Memory MCP's output via its SQLite file directly, or should we request/contribute a webhook or event-stream export upstream for cleaner sync semantics?
- **[Engineering]** Does Graphiti's existing ingestion API (episodes/facts) cleanly accept structural code entities (functions, call edges) as first-class facts, or does the sync layer need a schema adaptation step? This is the main technical risk in P0-2 and should be resolved in the spike below before further build.
- **[Engineering]** What's the actual incremental-reindex latency at scale (multi-million-LOC monorepo, high commit velocity) once both systems are chained together? Needs a load test before committing to the 5-minute SLA in P0-3 — latency is now the sum of Codebase-Memory MCP's index time plus Graphiti's write time plus sync overhead.
- **[Engineering]** How do we handle version drift as Codebase-Memory MCP and Graphiti each ship upstream releases independently? Need a compatibility-testing process so a dependency upgrade doesn't silently break the sync layer.
- **[Product]** Should the MCP query surface mirror Codebase-Memory MCP's existing 14–15 tools 1:1, or is a smaller, opinionated set better for v1 adoption?
- **[Legal/Security]** For design partners with strict source-code handling policies, what's required to pass security review beyond "self-hosted, no data leaves the network"? Need a SOC 2 posture decision even for a v1 pilot.
- **[Design partners]** Which 3–5 languages should the initial supported set be, based on actual design partner stacks? Currently assumed as Python/TypeScript/Go/Java pending partner confirmation.
- **[Product]** What counts as a "commit" trigger in orgs using non-Git-native workflows (e.g., Perforce, monorepo tooling with custom merge queues)? May affect P0-3 scope.

## Timeline Considerations

- No hard external deadline identified yet — this is a net-new product, not a contractual commitment. Recommend treating the 5-design-partner goal (Goal 5) as the primary timeline forcing function.
- **Dependency:** The entire v1 timeline hinges on P0-2 (the sync layer) being feasible against both projects' existing APIs without patching either. This should be de-risked before anything else — it's integration risk, not build risk, but it's still the thing that can blow up the schedule.
- **Suggested phasing:**
  - Phase 0 (1–2 weeks): Integration spike — stand up Codebase-Memory MCP and Graphiti independently against one open-source or internal repo, then hand-wire a minimal sync of a handful of facts to confirm the bi-temporal mapping actually works end to end using only public APIs. Kill/pivot decision point: if this requires forking either project, stop and re-scope rather than proceeding into Phase 1.
  - Phase 1 (3–5 weeks): Build the real sync layer, unified MCP surface, and packaging (P0-1 through P0-6) against 1–2 design partner repos, closed pilot.
  - Phase 2 (4 weeks): Harden based on pilot feedback, expand to remaining design partners, add P1 items as capacity allows.
- Wedge 2 (ACE) and Wedge 4 (CI enforcement) should not start until v1 has at least one design partner reporting trust in the graph — building governance on top of an unproven memory layer risks compounding the wrong foundation.
