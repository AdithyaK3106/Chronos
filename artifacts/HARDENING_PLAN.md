# Hardening plan, 2026-11-04

These are the decisions the user made, with their answers:

- Commands Chronos can't read → **ask**. Hook crashes or timeouts → **ask**.
- Threat rules on by default: destructive commands and git damage, sending data out and secrets, cheating on checks, production, and dependencies. Also look up more patterns, because this is a production app.
- Live attack test: real Claude Code with **Sonnet**, covering every threat area, not only `.env`.
- CodeScout: run the **real** CodeScout-1.7B model, on the GTX 750 Ti if it works, otherwise on the CPU. Test it on 20 public-repo prompts with no file names or file references.
- Benchmark corpus: **Python, JS/TS and Go** public repos. A manifest lives in git and the clones are cached outside it. **50 tasks** stored, 20 used now.
- Memory: check that it works as intended and whether it actually helps.

## Work items

- [x] 1. Shell analysis: catch hidden execution (piping into a shell, decode-then-run, inline `-c` code, `find -exec`/`-delete`, `$'..'` escapes, globs matching secret files, recursive reads over secret files). All of these → ask.
- [x] 2. Threat rule packs for commands (`chronos/security/command_rules.py`), each rule with an id, category and decision, and overridable from policy:
  - destructive commands
  - git damage
  - sending data out
  - credential stores and token commands
  - production, infrastructure and databases
  - dependencies
  - changes to the developer's machine
- [x] 3. Rules on file paths and content for Edit/Write:
  - `.chronos/**` → deny
  - agent settings, CI, git hooks, lockfiles → ask
  - added test-skip markers → ask
  - extra secret file types (`.tfstate`, `.netrc`, `.p12`, ...)
- [x] 4. Hook failures: crash → ask, internal deadline before Claude Code's timeout, `onFailure: block` for failures our code can't catch (Python missing, killed, bad output), an error log, and errors shown in the overview.
- [x] 5. Warn the agent when tool output contains instructions aimed at AI agents (PostToolUse `additionalContext`). Done in dda9d6a (Bash/Read/WebFetch/MCP; 0 false positives on 459 real docs).
- [x] 6. Make the OpenHands firewall apply the same command rules.
- [~] 7. Memory: audited and recall precision fixed (unrelated injection 93% → 0%). NOT yet measured: whether memory helps an agent finish tasks (needs a sequential-task A/B). Claude Code never writes memories automatically. 2026-11-05: memory A/B (`benchmarks/memory_ab.py`) running; see the morning report.
- [x] 8. Public benchmark corpus (3f98a47, public_v1, 50 tasks): manifest, fetch script, prompts without file names, and gold files and functions for any language.
- [x] 9. CodeScout on the GPU or CPU: compare against the deterministic resolver on 20 prompts. Result: 0/20 gold files in every configuration vs BM25 8/20 top-3 (`artifacts/codescout_eval_2026-11-04.json`); dropped.
- [x] 10. Live Claude Code (Sonnet) attack test across all threat areas. Run 1 and run 2 (0977318): baseline harmed 1/7, Chronos 0/7 in run 2.
- [ ] 11. Report.
