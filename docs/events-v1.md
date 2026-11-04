# Chronos events, v1

One JSON format for everything Chronos observes or decides: tool calls, policy decisions, sessions
and usage. The Claude Code hooks write it today; the Codex adapter and the team control plane
(both planned) will produce and consume the same records.

This document has two layers:

- **v0, what the audit log records today.** Every field and value below under "v0" is written by
  the current code (`chronos/audit/logger.py`, `append_audit_event`). Nothing here changes it.
- **v1, the additions** that make records portable across agents and machines. They are additive:
  a v1 reader accepts v0 records (see [Reading v0 records](#reading-v0-records)).

Status: v0 is implemented. The v1 fields are a proposal; no code writes them yet.

## Where events live

One append-only JSON Lines file per repository, outside the repository so the agent's tools and
`git clean` can't reach it:

| OS | Path |
|---|---|
| Linux | `~/.local/share/chronos/audit/<repo>-<id>.log` |
| macOS | `~/Library/Application Support/chronos/audit/<repo>-<id>.log` |
| Windows | `%LOCALAPPDATA%\chronos\audit\<repo>-<id>.log` |

`CHRONOS_DATA_DIR` overrides the base directory. `chronos audit verify` checks the hash chain.

## Record (v0, written today)

| Field | Type | Meaning |
|---|---|---|
| `timestamp` | string, ISO 8601 UTC with offset | When the record was written, e.g. `2026-11-04T16:53:13.029047+00:00` |
| `event_type` | string | What kind of event (table below) |
| `agent_id` | string | Who acted: `claude_code` (or `$CHRONOS_AGENT_ID`), an OpenHands agent id, `cli_user`, `dashboard_user` |
| `action` | string | The operation: `bash`, `file_editor`, a tool name (`Read`, `WebFetch`, `mcp__github__create_issue`), `approve`, `acquire_lock`, ... |
| `target` | string, at most 500 chars | What it acted on: a repo-relative path, a command line, a URL, a lock id, a pattern |
| `status` | string | Outcome (enumeration below) |
| `reason` | string | Human-readable explanation, shown to the developer |
| `metadata` | object | Type-specific details (below). Always present, may be `{}` |
| `prev_hash` | string, 64 hex | `hash` of the previous record; 64 zeros for the first |
| `hash` | string, 64 hex | SHA-256 chain hash of this record (below) |

`target`, `reason` and `metadata` pass through `redact_secrets` before writing: values that look
like API keys, tokens or passwords are replaced, so the log can be shared.

### Hash chain

```
payload   = the record without "hash"            (prev_hash included)
canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
hash      = sha256( prev_hash_utf8 || canonical_utf8 ).hexdigest()
```

Field order on disk doesn't matter. Writers hold an inter-process lock while reading the last hash
and appending, so concurrent hooks don't fork the chain. Any edit, deletion or reordering of a
record breaks verification from that record on.

### `event_type` values written today

| `event_type` | Kind (v1) | Written by | `status` values |
|---|---|---|---|
| `tool_authorization` | decision | firewall, Claude Code hooks (file rules, shell rules, MCP/WebFetch, outside-repo writes, hook failures) | `allow`, `ask`, `deny`, `error` |
| `tool_violation` | decision | PostToolUse backstop: a Bash command changed a protected/secret file without approval | `violation` |
| `planted_instruction` | decision | PostToolUse: tool output contains text aimed at AI agents | `warned` |
| `gate_approval` | decision | `chronos approve` / `revoke`, `chronos gate grant` / `revoke` | `granted`, `revoked`, `not_found` |
| `gate_rejection` | decision | dashboard: a human rejected a pending approval | `DENY` (see casing) |
| `lock_acquire`, `lock_release`, `lock_conflict` | tool_call | MCP server and dashboard (multi-agent node locks) | `allow`, `deny`; dashboard: `ALLOW`, `DENY` |
| `memory_recorded` | session | post-task memory distillation | `allow`, `error` |
| `memory_remember` | tool_call | dashboard: a lesson added by hand | `ALLOW` |

Run telemetry is separate. `chronos run` and the benchmarks write `run_started`,
`run_success` / `run_failed` / `run_cancelled` and `context_envelope_created` to the SQLite table
`run_events` (`event_id`, `run_id`, `seq`, `timestamp`, `event_type`, `payload`), not to the audit
log. v1 `session` and `usage` records take their fields from these.

**Casing.** The dashboard writes `ALLOW` / `DENY` in upper case; everything else is lower case.
Readers must compare case-insensitively. v1 writers use lower case only.

### `metadata` by event type (today)

| `event_type` | Keys |
|---|---|
| `tool_authorization` | `category`: the check that decided (`allow`, `permission`, `gate`, `lock`, `rule`, `error`, or a rule family: `secrets`, `git`, `integrity`, `dependencies`, `network`, `hidden`, ...); `rule`: rule id such as `git.push_force` (rule hits only); `secrets`: list of names (secret denials) |
| `tool_violation` | `category: "backstop"`, `kind`: `secret` or `protected`, `command` (500 chars) |
| `planted_instruction` | `kinds`: subset of `override`, `addressed`, `exfiltrate`, `conceal`; `excerpts`: up to 3 strings of at most 200 chars |
| `gate_approval` | `expires_at` (ISO UTC or null) and `approved_by` on grants; `revoked_by` on revokes |
| `run_events`: `run_started` | payload: `task_id`, `task_description`, `model_id`, `arm`, `suite`, `started_at` |
| `run_events`: `run_<status>` | payload: `status`, `duration_seconds`, `total_tokens`, `error_message` |
| `run_events`: `context_envelope_created` | payload: `envelope_id`, `token_budget`, `actual_tokens`, `files_count`, `symbols_count`, `tests_count` |

### Example (a real record)

Copied from a local audit log on 2026-11-04 (a test repository): a Bash command that edits the CI
workflow was held for approval.

```json
{
  "timestamp": "2026-11-04T16:53:13.029047+00:00",
  "event_type": "tool_authorization",
  "agent_id": "claude_code",
  "action": "bash",
  "target": "sed -i 's/pip install pytest/pip install -r requirements.txt pytest/' .github/workflows/ci.yml",
  "status": "ask",
  "reason": ".github/workflows/ci.yml changes the CI pipeline that checks every change",
  "metadata": {"category": "integrity", "rule": "integrity.ci_config"},
  "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
  "hash": "e5b8ba68162c9be524952c5dd473fd2455795107519897eb7b043123191782f2"
}
```

## v1 additions (proposed)

Six top-level fields. All are included in the hashed payload like any other field.

| Field | Type | Meaning |
|---|---|---|
| `schema` | string | `"chronos.event/1"`. Absent means v0 |
| `event_id` | string | ULID, unique per record. Lets the control plane deduplicate re-sent records |
| `kind` | string | One of `tool_call`, `decision`, `session`, `usage` (mapping in the table above) |
| `source` | object | `{"adapter": "claude_code" \| "codex" \| "openhands" \| "cli" \| "dashboard", "adapter_version": "...", "chronos_version": "...", "host": "<hostname hash>"}` |
| `session_id` | string or null | The agent's session id (Claude Code's `session_id`, Codex's conversation id). Today it is only used for Bash snapshots, not recorded |
| `tool_use_id` | string or null | Ties a PreToolUse decision to its PostToolUse result |
| `repo` | object | `{"id": "<repo id as in the log file name>", "remote": "<origin URL without credentials>" \| null}` |

### The four kinds

**`tool_call`.** The agent invoked a tool. v1 adds one record per call (today only decisions are
logged, plus the lock and memory operations above):

```json
{"schema": "chronos.event/1", "kind": "tool_call", "event_type": "tool_call",
 "action": "Edit", "target": "src/auth/login.py", "status": "allow", "reason": "",
 "metadata": {"phase": "pre", "input_digest": "sha256:..."}, "...": "common fields"}
```

`metadata.phase` is `pre` or `post`; `input_digest` is a hash, never the raw input, so file contents
don't leave the machine.

**`decision`.** Chronos allowed, asked, denied, warned or flagged something. These are the
`tool_authorization`, `tool_violation`, `planted_instruction`, `gate_approval` and `gate_rejection`
records above, unchanged apart from the common v1 fields.

**`session`.** Start and end of an agent session:

```json
{"schema": "chronos.event/1", "kind": "session", "event_type": "session_start",
 "action": "session", "target": "<repo id>", "status": "allow", "reason": "",
 "metadata": {"agent": "claude_code", "model": "claude-sonnet-...", "hooks_installed": ["PreToolUse", "PostToolUse", "UserPromptSubmit"]}}
```

`session_end` adds `metadata.duration_s`, `metadata.decisions` (counts per status) and
`metadata.files_touched`.

**`usage`.** Tokens and cost, for spend reports:

```json
{"schema": "chronos.event/1", "kind": "usage", "event_type": "usage",
 "action": "model_call", "target": "claude-sonnet-...", "status": "allow", "reason": "",
 "metadata": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0, "period": "session"}}
```

Today usage exists only for benchmark runs (`run_events` in SQLite, `chronos/telemetry/runs.py`),
including the context envelope's token budget; nothing reads a Claude Code or Codex session's own
token use yet.

## Versioning

- `schema` names the major version. Adding an optional field or a new `event_type`, `status`,
  `metadata` key or rule id is not a version change.
- Removing or renaming a field, changing a type, or changing the hash computation requires
  `chronos.event/2`.
- Readers must ignore unknown fields and unknown `metadata` keys, and treat an unknown `event_type`
  as an opaque record (still verify its hash).
- One log may mix versions. The hash chain covers whatever fields each record has.

## Reading v0 records

A v1 reader maps a record without `schema` like this:

| v1 field | From a v0 record |
|---|---|
| `schema` | `"chronos.event/0"` |
| `event_id` | `hash` (unique within a chain) |
| `kind` | from `event_type` (table above); unknown types are `decision` |
| `source.adapter` | `claude_code` if `agent_id` is `claude_code`, `cli` for `cli_user`, `dashboard` for `dashboard_user`, otherwise `openhands` |
| `session_id`, `tool_use_id` | `null` |
| `repo.id` | from the log file name |
| `status` | lower-cased |
