# Overnight queue, 2026-11-04 → 05

User approvals for tonight:
- Run the live Sonnet test, all 14 runs (about $1). This is a one-off exception to the overnight cap of 3 Haiku runs.
- Push to `tanish` at the end. Never `master`, never delete anything, never publish.
- Gemini benchmarks are fine.

## A. Finish and act on tonight's tests
- [ ] 1. Finish the memory A/B (`benchmarks/memory_ab.py`, results in `benchmarks/runs/memory_v1`). Re-run any `MODEL_API_ERROR` runs, then save `artifacts/memory_ab_2026-11-04.json`.
- [ ] 2. Finish the embedding test (`benchmarks/embed_eval.py`, log in `scratchpad/embed_eval.log`). If a model beats BM25 on eval20, check it on the 30 held-out tasks (`--tasks heldout`). Only if it still wins, build it in as an optional retriever: off by default, Ollama backend, background index, incremental.
- [ ] 3. Memory, depending on the A/B result. If memory helps: auto-capture in Claude Code (Stop/SessionEnd hook). If it doesn't: memory injection off by default.
- [ ] 4. Planted-instruction warning: PostToolUse `additionalContext` when tool output contains instructions aimed at AI agents.
- [ ] 5. Re-run the live Sonnet test with the fixed rules: `python -m benchmarks.live_claude.run --model sonnet --out artifacts/live_claude_2026-11-05.json`.

## B. Implementation-plan items (chronos-build-procedure.md, steps 0–2)
- [ ] 6. Change the install URL from `chronos-agent/chronos` to `AdithyaK3106/chronos` in install.sh, README and GUIDE.
- [ ] 7. Make the install.sh MCP step back up and merge the Cursor and Claude Desktop JSON configs instead of overwriting them. Add an installer test.
- [ ] 8. In gates.py, support the documented `policies:` format (`mode`, `denial_reason`). Reject unknown policy keys loudly.
- [ ] 9. Stop the protected patterns from blocking `docs/oauth.md` and `src/authors/list.py`. Add a test.
- [ ] 10. Add `chronos approve` / `chronos revoke` aliases that write audit events.
- [ ] 11. Deny agent writes to `.codex/**`. Already covered: `.chronos/**` and `.claude/settings*.json`.
- [ ] 12. Write `docs/events-v1.md`, the shared event format: tool calls, decisions, sessions, usage.
- [ ] 13. Add `chronos claude install --managed`, which generates managed-settings.json (`onFailure: block`, `allowManagedHooksOnly`). Warn on Claude Code versions below 2.1.295.
- [ ] 14. Bring the README claims in line with the benchmark numbers.

## C. Wrap up
- [ ] 15. Morning report as an artifact: what changed, all numbers, what didn't work.
- [ ] 16. Run the full test suite, then push to `tanish`.

## State at the start
- CodeScout verdict: drop it. It got 0/20 in every configuration tried. BM25 today: 8/20 file in top 3 on vague prompts from public repos.
- `uv.lock` was modified by something else (a relock for chronos-agent 0.3.0). Not committed; leave it.
- The Ollama server runs from /data/tools (start: `OLLAMA_MODELS=/data/tools/ollama-models /data/tools/ollama/bin/ollama serve`).
- Scratch venv with numpy, torch and sentence-transformers: `scratchpad/embvenv`.
