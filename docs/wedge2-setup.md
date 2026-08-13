# Wedge 2 setup — Packmind + the playbook loop

Wedge 2 turns agent mistakes into coding standards. Packmind stores and distributes
the rules; Chronos supplies the Reflector/Curator loop and the Wedge 1 evidence.

## 1. Run Packmind locally

Packmind OSS (Apache 2.0) ships a Docker Compose stack.

```bash
git clone https://github.com/PackmindHub/packmind
cd packmind
docker compose up -d          # postgres, redis, backend, frontend, nginx
```

The stack serves HTTPS on `https://localhost` (nginx, self-signed cert). The API
lives under `/api/v0`. Bring it down with `docker compose down`.

On Windows, clone with long paths enabled or the checkout fails partway:
`git config --global core.longpaths true`.

## 2. Create an org, space, and API key

1. Open `https://localhost`, sign up, and create an organization and a space.
2. Generate an API key in the UI (backed by `POST /auth/api-key/generate`).

## 3. Point Chronos at it

```bash
export PACKMIND_API_URL=https://localhost   # no trailing /api/v0
export PACKMIND_API_KEY=<key from step 2>
export PACKMIND_ORG_ID=<optional>           # else resolved from /auth/me
export PACKMIND_SPACE_ID=<optional>         # else the first space on the account
export CHRONOS_LLM_MODEL=openai/gpt-4o-mini # any litellm model id
export CHRONOS_EMBED_MODEL=text-embedding-3-small
```

The LLM is called through litellm, so an `OPENAI_API_KEY` (or the equivalent for
whichever provider `CHRONOS_LLM_MODEL` names) must also be set. Unlike Wedges 1
and 3, **Wedge 2 does need an LLM** — reflection is the feature.

## 4. Verify

```bash
chronos doctor
```

```
packmind    : ok | 4 rules | last proposal 2026-08-13T10:00:00+00:00
```

Other states: `not configured (...)` when the env vars are unset, and
`UNREACHABLE ...` when they are set but the API refuses.

## 5. Register the MCP server

```json
{ "mcpServers": {
    "chronos-playbook": {
      "command": "chronos-playbook-mcp",
      "env": { "CHRONOS_GROUP_ID": "myrepo",
               "PACKMIND_API_URL": "https://localhost",
               "PACKMIND_API_KEY": "..." }
    } } }
```

Tools: `chronos_capture_lesson`, `chronos_query_playbook`,
`chronos_propose_rule`, `chronos_playbook_health`.

## How approval works — read this before wiring an agent

Packmind's OSS API has **no rule-proposal object and no status field**. A
`Standard` is created live. What it does have is a separate distribution step:

```
POST .../standards                        create   (inert — reaches nobody)
POST .../deployments/standards/publish    publish  (writes CLAUDE.md,
                                                    .cursor/rules, copilot-instructions)
```

So *not publishing* is the approval gate. **Chronos never calls the publish
endpoint.** Rules it proposes sit in Packmind unpublished until a human reviews
and publishes them from the UI. This is the PRD's "human approval required"
requirement expressed in Packmind's real lifecycle rather than an invented field.

Full API notes and every deviation are documented at the top of
`chronos/playbook.py`.

## Failure behaviour

If Packmind is unreachable, `chronos_capture_lesson` and `chronos_query_playbook`
raise `PackmindError` naming this file. They do not silently discard the trace —
a lost lesson is worse than a loud error.
