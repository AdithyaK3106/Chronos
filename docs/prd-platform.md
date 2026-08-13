# PRD: Chronos Context OS — Full Platform (Wedges 1–4)

**Status:** Draft for review
**Scope:** Platform-level roadmap covering all four strategic wedges. Companion to, not a replacement for, the [v1 PRD](prd-v1.md), which covers Wedge 1 in build-level detail.
**Audience:** Engineering/product (build planning), investors (roadmap and defensibility), design partners (what's coming and when).
**Build philosophy:** Chronos is an **integration and orchestration layer**, not a from-scratch platform. Every wedge below is scoped around a specific existing open-source project doing the heavy lifting; Chronos's job is the glue, the unified interface, and the parts that don't exist anywhere yet. Where a wedge turns out to require rebuilding something an existing project already does well, that's a signal to re-scope the wedge, not a green light to build it in-house.
**Sequencing:** Wedge 1 → Wedge 3 → Wedge 2 → Wedge 4. Rationale in Timeline Considerations.

---

## Problem Statement

As engineering orgs scale from a handful of AI coding agent pilots to dozens of agents (human and autonomous) working concurrently across many repos, four distinct failure modes compound: agents can't tell current code state from deprecated state (temporal hallucination), agents overwrite each other's implicit decisions in shared codebases (cross-session causal blindness), organizational coding standards live in static files that rot the moment the codebase moves on (context drift), and there's no enforcement mechanism to stop an agent from merging code that violates all of the above before it reaches production. No single existing tool solves all four; each is separately being solved by different, mature open-source and commercial projects. The opportunity is not to out-build these projects but to be the layer that makes them aware of each other — specifically, to make policy governance and enforcement temporally and structurally aware via the bi-temporal graph, rather than operating blind to code history.

## Goals

1. Ship a working integration across all four wedges within [N] quarters, with each wedge validated against real design partner usage before the next wedge begins active development (see Timeline).
2. Keep proprietary build surface area to the minimum needed to connect existing frameworks — target >70% of end-to-end functionality delivered by adopted OSS projects, with Chronos-authored code concentrated in sync/mapping layers and the unified interface.
3. Demonstrate that connecting these frameworks produces capability none of them has alone (e.g., temporally-aware policy enforcement, provenance-linked playbook learning) — this compounded capability, not any single wedge, is the actual product thesis.
4. Land and retain design partners incrementally: a partner should get standalone value from Wedge 1 alone, additional value from Wedge 3 layered on, and so on — no wedge should require the full stack to be useful.
5. Preserve switching cost as the moat (per the original thesis): even though components are OSS, the compounding intent/provenance/playbook history captured through Chronos's connective layer is not portable to a competitor standing up the same OSS projects from scratch.

## Non-Goals

- **Building a competing AST parser, temporal graph engine, agent framework, or policy engine from scratch.** If a wedge appears to need this, treat it as a signal the wedge is mis-scoped, not a build task. Reassess against the current OSS landscape before writing new core infrastructure.
- **Forking any of the adopted OSS dependencies as a default posture.** Patches are proposed upstream first; forking is a last resort reserved for cases where upstream is unresponsive and the gap blocks a committed customer requirement.
- **Replacing existing developer-facing agent tools (Claude Code, Cursor, Copilot, Devin, etc.).** Chronos is infrastructure these tools plug into via MCP, not a competing IDE or agent.
- **A hosted multi-tenant SaaS in this phase.** Every wedge assumes self-hosted deployment within the customer's environment, consistent with the security posture of the adopted OSS projects (Codebase-Memory MCP, Packmind OSS, ast-grep/Opengrep, OPA) and the v1 PRD.
- **Non-code organizational memory ingestion (Slack, Jira, general knowledge management)** beyond what's needed to feed the wedges below. This remains a large, separate surface area.

## Build vs. Buy: Framework Mapping by Wedge

This is the core scoping decision for the whole roadmap. Each wedge maps to a primary existing framework and a specific integration gap Chronos fills.

| Wedge | What it needs to do | Primary existing framework(s) | What Chronos actually builds |
|---|---|---|---|
| **1. Bi-Temporal AST Graph** | Structural code graph with valid/transaction time tracking | [Codebase-Memory MCP](https://github.com/DeusData/codebase-memory-mcp) (AST parsing, structural graph, MCP tool surface) + [Graphiti](https://github.com/getzep/graphiti) (bi-temporal knowledge graph engine, `valid_at`/`invalid_at` primitives) | Sync layer mapping Codebase-Memory MCP's structural diffs into Graphiti's fact model; unified MCP query surface routing current-state vs. as-of queries to the right backend. See v1 PRD for full detail. |
| **3. Intent & Provenance Ledger** | Register write-intent across concurrent agents; surface conflicts before they cause silent divergence | [Forge Orchestrator](https://htdocs.dev/posts/from-conductor-to-orchestrator-a-practical-guide-to-multi-agent-coding-in-2026/) (single-binary file locking, drift detection, cross-tool knowledge persistence across Claude Code/Codex/Gemini CLI) as the coordination substrate; design informed by prior art in shared-ledger coordination (e.g., Magentic-One's orchestrator ledger pattern, Claude Code Agent Teams' shared task list with file locking) | Extend Forge Orchestrator's file-locking primitive from file-level to **AST-node-level**, using Wedge 1's structural graph to resolve "adjacent node" conflicts that file-level locking misses (e.g., two agents touching different functions that call each other). Provenance query API tying a code change back to the registered intent. |
| **2. Agentic Context Engineering (Policy Playbook)** | Self-improving, execution-feedback-driven coding standards playbook | [Packmind OSS](https://github.com/PackmindHub/packmind) (playbook capture, versioning, multi-agent distribution to CLAUDE.md/.cursor/rules/copilot-instructions, drift tracking) for governance/distribution + [Kayba's `agentic-context-engine`](https://github.com/kayba-ai/agentic-context-engine) (open-source ACE implementation: Generator/Reflector/Curator loop, Apache 2.0) for the self-improving loop | Feed Kayba ACE's Reflector/Curator output into Packmind's playbook as versioned rule proposals (using Packmind's existing update-proposal workflow), rather than building a separate playbook store. Chronos's addition: ground ACE's reflection step in Wedge 1's bi-temporal graph so lessons are tagged with the exact commit/timeframe they were learned from. |
| **4. Executable Policy Governance (CI Enforcement)** | Block merges that violate learned standards or use deprecated patterns | [ast-grep](https://ast-grep.github.io/) (MIT) or [Opengrep](https://opengrep.dev/) (the vendor-coalition fork of Semgrep's engine, created to escape Semgrep's Dec-2024 rules-licensing change) for AST-pattern-based detection, plus [Open Policy Agent (OPA)](https://www.openpolicyagent.org/) (Apache 2.0) as the policy-as-code gating layer | **Deliberately built in-house, not licensed from Packmind Enterprise** — see rationale below. Chronos builds: (1) an LLM-assisted rule generator that turns a Wedge 2 playbook rule into an ast-grep/Opengrep pattern, including a "is this rule automatable" gate modeled on Packmind's detectability check; (2) the OPA policy layer that decides block/warn/pass; (3) the differentiator neither OSS tool nor Packmind's static lint has — wiring Wedge 1's live temporal state into the check, so "deprecated pattern" is a real-time graph query, not a rule that itself goes stale; (4) provenance tie-in to Wedge 3 so a blocked violation traces back to the agent/intent that produced it. |

**Why Wedge 4 is a build, not a buy, unlike the other three wedges:** the original plan was to license Packmind Enterprise's `packmind-cli lint`, but that capability sits behind a paid, license-key-gated product, not Packmind OSS — depending on it commercially would mean a reseller/OEM relationship with a third party for the wedge that's supposed to be part of the core moat. The underlying hard problems (pattern matching, policy evaluation) are solved by permissively-licensed OSS (ast-grep/Opengrep, OPA); what's missing is just the rule-generation and temporal-awareness layer on top, which is squarely inside Chronos's own differentiated build surface — and arguably *more* defensible built in-house, since a static lint tool bought from a vendor was never going to be the moat anyway.

**Reading this table the way it's meant to be read:** Wedges 1–3 are majority-covered by mature existing projects (Codebase-Memory MCP, Graphiti, Forge Orchestrator, Packmind, ACE); Chronos's engineering effort there is concentrated in connective tissue, not re-deriving those capabilities. Wedge 4 is the one deliberate exception — it still leans on existing OSS for the two hard sub-problems (pattern matching via ast-grep/Opengrep, policy evaluation via OPA), but the rule-generation and temporal-enforcement layer is built in-house rather than licensed, because that's the part of the roadmap most exposed to third-party commercial dependency risk if bought instead.

## User Stories

**Wedge 1 — Platform engineer (see v1 PRD for full detail)**
- As a platform engineer, I want a queryable, temporally-aware structural graph of my codebase so agents stop hallucinating deprecated patterns.

**Wedge 3 — Platform engineer / orchestrator agent**
- As a platform engineer running concurrent agents (Claude Code, Codex, Devin) against the same repo, I want conflicting writes on related code to surface before they land, so I don't get silent architectural divergence between agents.
- As an orchestrator agent, I want to register intent against specific AST nodes (not just files) before starting a task, so a second agent working on a related function gets warned even if it's touching a different file.
- As a platform engineer, I want a provenance query — "why was this function changed, and by what agent, under what registered intent" — so I can debug agent-introduced regressions.

**Wedge 2 — Tech lead / platform engineer**
- As a tech lead, I want our coding standards playbook to automatically incorporate lessons from agent failures (a specific lint error, a rejected PR pattern) without me manually rewriting CLAUDE.md every time, so the standards stay current with zero maintenance overhead.
- As a platform engineer, I want new playbook rules proposed by the ACE learning loop to go through the same review/approval workflow as human-authored rules, so nothing silently changes agent behavior without oversight.

**Wedge 4 — Platform engineer / engineering leadership**
- As a platform engineer, I want a PR that reintroduces a pattern the graph knows is deprecated to be blocked automatically before human review, so reviewers stop catching the same class of mistake repeatedly.
- As engineering leadership, I want a report of what got blocked and why, so I can demonstrate the safety case for expanding agent usage to the rest of the org.

## Requirements (by wedge, high level — see v1 PRD for Wedge 1 P0/P1/P2 detail)

### Wedge 1: Bi-Temporal AST Graph
Full requirements in the v1 PRD. Summary: adopt Codebase-Memory MCP for parsing, adopt Graphiti for bi-temporal storage, build the sync layer and unified MCP surface. No new parsing or temporal-engine code.

### Wedge 3: Intent & Provenance Ledger
- **P0:** Integrate Forge Orchestrator (or equivalent) as the base coordination/locking layer; confirm its file-locking model and evaluate extension points before writing any new locking logic.
- **P0:** Extend locking granularity from file-level to AST-node-level using Wedge 1's graph to identify when two agents' file-level-independent changes are actually structurally adjacent (e.g., caller/callee across files).
- **P0:** Provenance query API: given a code node, return the registered intent, agent, and timestamp of the change that produced its current state.
- **P1:** Real-time conflict notification injected into a second agent's context when a conflicting intent is detected (rather than only surfaced after the fact).
- **P2:** Cross-repo intent tracking for organizations running agents across microservice boundaries.

### Wedge 2: Agentic Context Engineering (Policy Playbook)
- **P0:** Deploy Packmind OSS as the playbook store and distribution mechanism — no new playbook storage or CLAUDE.md/.cursor/rules rendering logic.
- **P0:** Deploy Kayba's `agentic-context-engine` as the Reflector/Curator loop; wire its output into Packmind's existing update-proposal workflow rather than a bespoke approval flow.
- **P0:** Tag ACE-generated playbook proposals with the Wedge 1 commit/timestamp context they were learned from, so reviewers can see exactly which failure produced which rule.
- **P1:** Playbook rule effectiveness tracking (helpful/harmful counters, per ACE's design) surfaced back through Packmind's adoption-tracking UI.

### Wedge 4: Executable Policy Governance (CI Enforcement)
- **P0:** Adopt ast-grep or Opengrep as the pattern-matching engine and OPA as the policy/gating layer — no new parser or general-purpose policy language is written.
- **P0:** Build the rule-generation step: given a Wedge 2 playbook rule in plain language, use an LLM to emit an ast-grep/Opengrep pattern, with an explicit "is this rule automatable" check (analogous to Packmind's detectability gate) so unenforceable rules are flagged for rewrite rather than silently skipped.
- **P0:** Wire Wedge 1's live temporal state into the enforcement check, so "is this pattern deprecated" is a real-time graph query against current valid state, not a static rule that itself goes stale.
- **P0:** Wire Wedge 3's provenance ledger into enforcement results, so a blocked violation is traceable to the agent and registered intent that produced it.
- **P1:** Enforcement audit report for engineering leadership (what was blocked, why, trend over time).
- **P1:** Human review/override workflow for generated rules before they go live in blocking mode, given the rules are LLM-generated — a rule should default to warn-only until a human approves it for hard blocking.

## Success Metrics

- **Integration ratio:** % of each wedge's functionality delivered by adopted OSS vs. Chronos-authored code (target: track and report per wedge; flag any wedge trending toward >50% custom code as a scope-review trigger).
- **Cross-wedge capability:** at least one demonstrable capability per wedge pair that neither underlying OSS project could do alone (e.g., temporal-aware CI blocking, provenance-linked playbook learning) — this is the actual differentiation thesis and should be validated, not assumed.
- **Design partner incremental adoption:** % of Wedge 1 design partners who adopt Wedge 3, then Wedge 2, then Wedge 4, without churning at any stage (validates the "each wedge stands alone" goal).
- Wedge-specific metrics carry over from the v1 PRD (Wedge 1) and should be defined at the same level of rigor before each subsequent wedge begins active build — not deferred to "we'll figure it out."

## Open Questions

- **[Engineering]** Has anyone validated that Forge Orchestrator's locking model can be extended to AST-node granularity without forking it, or does this require deeper access to its internals than its public interface exposes? This is the key technical risk for Wedge 3 and should be spiked before committing to the sequencing below.
- **[Engineering]** How reliable is LLM-generated ast-grep/Opengrep rule synthesis in practice — what's the false-positive/false-negative rate on a first pass, and does it need a human-in-the-loop review step before any rule is allowed to run in hard-blocking (vs. warn-only) mode? This should be validated with a small benchmark before committing Wedge 4's timeline.
- **[Legal]** ast-grep is MIT; Opengrep is a newer community fork — confirm its license terms and governance stability directly before depending on it, rather than assuming it inherited Semgrep's original LGPL 2.1 terms cleanly.
- **[Legal/Partnerships]** Building this heavily on other companies' OSS products (Codebase-Memory MCP, Graphiti, Packmind OSS, ast-grep/Opengrep, OPA) raises a general question worth resolving once, not per-wedge: at what point does depending on a given project warrant a formal relationship (sponsorship, support contract, or contribution commitment) rather than just consuming the public repo, especially for smaller or newer projects in the stack?
- **[Engineering]** Kayba's `agentic-context-engine` is Apache 2.0 with ~2.3K stars and one maintainer group (Kayba) — what's the risk assessment if that project stalls or changes license terms, given Wedge 2 depends on it directly?
- **[Product]** Does the "each wedge stands alone" goal (Goal 4) hold up in practice, or does Wedge 4 (enforcement) actually require Wedge 2 (playbook) to have real content first, making the two more coupled than the sequencing implies?

## Timeline Considerations

**Sequencing rationale (Wedge 1 → 3 → 2 → 4):**
- Wedge 1 must exist first — every other wedge either directly depends on the graph (3, 4) or benefits from grounding in it (2).
- Wedge 3 (intent ledger) is sequenced next because it's the most novel engineering risk (extending file-locking to AST-node granularity) and validating it early de-risks the roadmap; it also delivers standalone value to any design partner running concurrent agents, independent of Wedges 2/4.
- Wedge 2 (ACE playbook) follows — it's largely a matter of wiring two existing OSS projects (Packmind, Kayba's ACE) together, lower engineering risk than Wedge 3, but benefits from Wedge 1 being mature enough to ground reflections in real commit history.
- Wedge 4 (enforcement) is last both because enforcement without a trusted, populated playbook (Wedge 2) and a stable graph (Wedge 1) risks blocking merges on incomplete or unreliable signals — a fast way to lose design partner trust — and because, now that it's an in-house build rather than a licensing decision, it carries the most rule-generation-accuracy risk of any wedge and benefits most from being validated last, against a mature graph and playbook.

**Phasing:**
- **Phase 0 (Wedge 1, per v1 PRD):** Integration spike + build, ~5–7 weeks. Kill/pivot checkpoint if Codebase-Memory MCP + Graphiti integration requires forking either project.
- **Phase 1 (Wedge 3):** Spike Forge Orchestrator's extensibility (2 weeks) before committing to a build timeline. If AST-node-level locking isn't achievable without forking, re-scope Wedge 3 to file-level coordination only for v1 of this wedge.
- **Phase 2 (Wedge 2):** Lower-risk integration phase — Packmind OSS + Kayba ACE wiring, target 3–4 weeks given both are mature, documented OSS projects.
- **Phase 3 (Wedge 4):** Now a real build, not a licensing negotiation — budget more time than the other wedges. Start with a rule-generation accuracy spike (1–2 weeks) benchmarking LLM-generated ast-grep/Opengrep patterns against a hand-labeled rule set before committing to the full build; default all generated rules to warn-only until that accuracy bar is met.
- No hard external deadlines identified. Each phase gate should require design partner validation of the prior wedge before starting the next, per Goal 1 and Goal 4.
