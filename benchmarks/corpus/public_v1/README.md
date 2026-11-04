# Public corpus v1 (built 2026-11-04)

50 real issues from repositories Chronos's authors did not write, for localization, solve-rate and
token benchmarks. Built by `python -m benchmarks.corpus.build` from SWE-bench Lite (Python) and
SWE-bench Multilingual (JS/TS/Go); repositories are fetched on demand by `benchmarks.corpus.checkout`.

| Language | Tasks | In eval20 | Repositories |
|---|---|---|---|
| Python | 25 | 10 | requests, flask, pytest, pylint, seaborn, xarray, django |
| JavaScript | 8 | 4 | preact, axios |
| TypeScript | 5 | 1 | vuejs/core |
| Go | 12 | 5 | caddy, gin, prometheus, hugo |

Fields: `prompt_vague` (no file, path, function or class names; Gemini-written, checked
mechanically; plain-word file stems such as "blueprints" are allowed), `prompt_issue` (issue text
without code blocks, tracebacks, URLs or paths; may still contain code identifiers), `gold_files`,
`gold_symbols` (innermost function/method around each changed line; empty when the fix is
module-level), `patch`, `test_patch`, `fail_to_pass`, `eval20`.

Use the 30 tasks outside eval20 as a held-out set: do not tune on them.
