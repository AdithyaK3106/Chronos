# Claude Code hook payload fixtures

Real-shaped stdin payloads for `chronos hook claude <pre|post|prompt>`, one per file, following the
hook input format Claude Code sends (`session_id`, `transcript_path`, `cwd`, `permission_mode`,
`hook_event_name`, `tool_name`, `tool_input`, `tool_use_id`, plus `tool_response` for PostToolUse and
`prompt` for UserPromptSubmit).

`${REPO}` is replaced by the test with a temporary git repository containing `app.py`, `auth.py`,
`.env`, `README.md` and `.github/workflows/ci.yml`. `expected.json` maps each fixture to the outcome
`tests/test_hook_fixtures.py` asserts: `allow`, `ask`, `deny` (PreToolUse), `warn` / `silent`
(PostToolUse), `context` (UserPromptSubmit).

To add a case: drop a JSON file here and add its expected outcome.
