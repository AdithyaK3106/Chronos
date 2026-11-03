# Buy vs build: Cupcake vs the Chronos hook (4 Nov 2026)

Question: could [Cupcake](https://github.com/eqtylab/cupcake) (EQTY Lab, OPA/Rego policies compiled to
Wasm, Claude Code hooks) replace Chronos's own PreToolUse enforcement?

## Method

- Cupcake 0.5.2 built from source (commit `27b94b8`) with OPA 1.4.2. Everything was installed in
  `/data/chronos-bench-scratch/m6`. The repo pins Rust 1.91; I used the installed stable toolchain
  instead of letting rustup download 1.91.
- **Replay corpus:** 262 Claude Code PreToolUse events (the JSON Claude Code sends, including
  `transcript_path`) over the real file trees of clones of CNG (125) and Chronos (137): plain
  edits/reads, protected-module edits/reads, secret reads/writes (`.env`, keys, credentials), path
  escapes, Grep (plain and `*.env`), and new-file writes.
- Each event runs as a real process per call, as Claude Code does: `chronos hook claude pre` (dev
  branch) vs `cupcake eval --harness claude`. They are **interleaved**, alternating which goes first
  per event, so both see the same machine load. A benchmark was running at the same time, so the
  absolute latencies are higher than on an idle machine.
- **Two Cupcake configurations:**
  - **default**: `cupcake init --harness claude` as shipped.
  - **tuned**: built-ins configured to match Chronos's policy as closely as built-ins allow.
    `protected_paths` gets `**/*auth*`, `**/*policy*`, ... and `rulebook_security_guardrails` gets
    `.env`, `.pem`, `.key`, `id_rsa`, `credentials.json`, `secrets/`.

## Results

**Decisions** (counts over both repos; Chronos with its default policy):

| Event | n | Chronos | Cupcake default | Cupcake tuned |
|---|---|---|---|---|
| plain edit | 120 | allow | allow | allow (1 deny, false positive) |
| plain read / grep / new file | 82 | allow | allow | allow |
| protected-module edit | 14 | **ask** | allow | deny (halt) |
| protected-module read | 14 | allow | allow | allow |
| secret read / write | 24 | deny | **allow** | deny |
| Grep with `*.env` glob | 2 | deny | allow | **allow** |
| `/etc/shadow` edit | 2 | allow (outside repo) | deny | deny |
| `../../etc/passwd`, sibling repo | 4 | allow (outside repo) | allow | allow |

**Latency per call, interleaved** (p50 / p95 over the 4 runs): Chronos 310–315 / 400–464 ms;
Cupcake 253–274 / 341–352 ms. Cupcake recompiles its Rego to Wasm on every call (OPA build plus
~160 ms of Wasm init) and is still ~15% faster at the median. Profiling attributed ~0.24 s of a Chronos call to
`_find_repo` importing the pydantic models module, but making that import lazy did not reduce
end-to-end latency: the edit path needs the models anyway (tried, measured, reverted).

## Findings

1. **Out of the box, Cupcake protects the machine, not the repository:** system paths, its own
   config, git hooks. It allowed all 24 secret reads/writes and all 14 protected-module edits.
2. **Tuned, it matches Chronos on secrets** but cannot express Chronos's default for protected
   code: the `protected_paths` built-in only *halts*, and there is no "ask the developer" built-in.
   Custom Rego `ask` rules can express it (the engine supports `ask`); that is code we would write
   and maintain.
3. **Gaps in Cupcake's built-ins:**
   - **Grep with a secret glob is not covered** (Chronos denies it).
   - Guardrail patterns match as substrings, so `secrets/` also blocked `scripts/audit_secrets.py`.
4. **Chronos gap found by the same replay:** paths outside the repository are allowed by default
   (left to Claude Code's permissions; `CHRONOS_HOOK_STRICT_BOUNDARY=1` denies them). GUIDE.md wrongly
   said they were denied; fixed in `0e0cde4`.
5. What Cupcake has that Chronos lacks: a general policy language (Rego), signals, a watchdog
   (LLM-as-judge), and multi-harness support (Cursor, OpenCode, Factory). What it lacks relative to
   Chronos: code context injection from a code graph, memory, multi-agent locks, an audit hash
   chain, and the dashboard.

## Recommendation

**Keep building Chronos's enforcement; don't replace it with Cupcake.** At equal coverage we would
be writing the same rules in Rego instead of Python, and losing the protected-module "ask" default,
the Grep check and integration with locks/audit. Worth borrowing:
- Rego as an optional user-policy extension point (Cupcake shows the per-call cost is acceptable).
- Its system-path protections (`/etc/`, `~/.ssh/`) as part of a stricter default for paths outside
  the repo (see decision 4).

Not done: the **codebase-memory-mcp** arm. 0.12.1 refuses to start unless its settings directory has
no group-writable ancestor; on this machine that is only possible under the real home directory,
which was out of bounds tonight. See the morning report.
