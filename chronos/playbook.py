"""Packmind client — the playbook store for Wedge 2.

=============================================================================
RESEARCH NOTES (Step 0). Read from PackmindHub/packmind @ main, Apache 2.0.
Findings are from the TypeScript source; the REST API is not separately
documented. File references are into that repo, not this one.
=============================================================================

TRANSPORT
  NestJS REST API. Global prefix `/api/v0` (apps/api/src/main.ts:133).
  Auth: `Authorization: Bearer <api_key>` (apps/api/src/app/auth/auth.guard.ts:74).
  Keys are minted in the UI via POST /auth/api-key/generate.

NO MCP SERVER.  The PRD asked us to check. There isn't one — the only *mcp*
  files in the repo are Playwright tooling for their demo recorder. So we call
  raw HTTP. If they ship one later this module is the single place to swap.

RESOURCE HIERARCHY
  organization -> space -> standard -> rule
  A standard is a *document* (name, description, scope) holding N rules;
  a rule is essentially just `{content: string}`.

ENDPOINTS WE USE
  GET  /auth/me                                        -> org + space discovery
  GET  /organizations/{org}/spaces/{space}/standards    -> list
  POST /organizations/{org}/spaces/{space}/standards    -> create
       body: {name, description, rules: [{content}], scope?}
  GET  /organizations/{org}/spaces/{space}/standards/{id} -> detail incl. rules

DEVIATIONS FROM THE PRD — implemented as found, not papered over.

  [D1] There is no "rule proposal" object, and no status field.
       `Standard` is {id, name, slug, description, version, userId, scope,
       spaceId, movedTo, updatedAt} (packages/types/src/standards/Standard.ts).
       No status/state/approval field exists anywhere in the OSS API. The
       PRD's status="proposed" | "active" | "rejected" cannot be set.

  [D2] Creation and distribution are SEPARATE calls, and that is what gives
       us the approval gate for free.
         create  POST .../standards                      (inert)
         publish POST /organizations/{org}/deployments/standards/publish
                 body: {targetIds, standardVersionIds}   (writes CLAUDE.md,
                 .cursor/rules, copilot-instructions via git targets)
       A created-but-never-published standard is not distributed to any agent.
       So "proposed" == created and not published. Chronos therefore NEVER
       calls the publish endpoint; a human publishes from the Packmind UI.
       This is the PRD's human-approval requirement, honoured with Packmind's
       real lifecycle instead of an invented field.

  [D3] Consequently `packmind_proposal_id` in the PRD's return shape is a
       StandardId (a created, unpublished standard). Named as the PRD asked
       so callers are not surprised; it is a standard id.

  [D4] No semantic search endpoint. `chronos_query_playbook(topic)` filters
       client-side over the space's standards. Fine at OSS scale (tens to
       hundreds of standards); if a partner outgrows it, that is a Packmind
       feature request, not something to build around here.

GAP, DOCUMENTED AND NOT WORKED AROUND (per the Step-0 constraint)
  We store evidence (evidence_node/valid_at/commit_context) in the standard's
  `description`, because there is no custom-metadata or tags field on either
  Standard or Rule. It round-trips through a parseable EVIDENCE block. This is
  the one place we bend Packmind's model; the alternative was a Chronos-side
  store, which the constraints correctly forbid.
"""

import json
import os
import urllib.error
import urllib.request

EVIDENCE_MARK = "--- chronos evidence ---"


class PackmindError(RuntimeError):
    """Packmind is unreachable or refused. Never swallowed — a lost trace is
    worse than a loud failure (PRD Step 4)."""


def _setup_hint(detail):
    return PackmindError(f"Packmind not reachable — run docs/wedge2-setup.md ({detail})")


class Packmind:
    def __init__(self, url=None, key=None, org=None, space=None, timeout=15):
        self.url = (url or os.environ.get("PACKMIND_API_URL", "")).rstrip("/")
        self.key = key or os.environ.get("PACKMIND_API_KEY", "")
        self.org = org or os.environ.get("PACKMIND_ORG_ID", "")
        self.space = space or os.environ.get("PACKMIND_SPACE_ID", "")
        self.timeout = timeout
        if not self.url or not self.key:
            raise _setup_hint("PACKMIND_API_URL and PACKMIND_API_KEY must be set")

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            f"{self.url}/api/v0{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read().decode()
        except urllib.error.HTTPError as e:
            raise PackmindError(
                f"Packmind {method} {path} -> HTTP {e.code}: {e.read().decode()[:200]}"
            ) from e
        except (urllib.error.URLError, OSError) as e:
            raise _setup_hint(f"{method} {path}: {e}") from e
        return json.loads(raw) if raw else None

    def _scope(self):
        """Resolve org/space once, from /auth/me if not configured explicitly."""
        if not self.org or not self.space:
            me = self._call("GET", "/auth/me") or {}
            org = me.get("organization") or {}
            self.org = self.org or org.get("id") or ""
            spaces = me.get("spaces") or []
            if not self.space and spaces:
                self.space = spaces[0].get("id", "")
            if not self.org or not self.space:
                raise PackmindError(
                    "could not resolve org/space from /auth/me — "
                    "set PACKMIND_ORG_ID and PACKMIND_SPACE_ID"
                )
        return f"/organizations/{self.org}/spaces/{self.space}/standards"

    def list_rules(self):
        """Every standard in the space, with evidence parsed back out."""
        out = []
        for s in self._call("GET", self._scope()) or []:
            desc = s.get("description") or ""
            body, _, ev = desc.partition(EVIDENCE_MARK)
            rec = {
                "id": s.get("id"),
                "name": s.get("name"),
                "rule_text": body.strip() or s.get("name", ""),
                "version": s.get("version"),
            }
            try:
                rec.update(json.loads(ev.strip()) if ev.strip() else {})
            except json.JSONDecodeError:
                pass  # human-authored standard, no evidence block. Not an error.
            out.append(rec)
        return out

    def create_standard(self, rule_text, evidence):
        """Create an UNPUBLISHED standard. See [D2]: not publishing is the
        approval gate — a human publishes from the Packmind UI."""
        name = rule_text.strip().splitlines()[0][:80] or "chronos rule"
        desc = f"{rule_text}\n\n{EVIDENCE_MARK}\n{json.dumps(evidence, indent=2)}"
        r = self._call(
            "POST",
            self._scope(),
            {
                "name": name,
                "description": desc,
                "rules": [{"content": rule_text}],
                "scope": None,
            },
        )
        return (r or {}).get("id") or ((r or {}).get("standard") or {}).get("id")

    def health(self):
        try:
            rules = self.list_rules()
        except PackmindError as e:
            return {"status": "unreachable", "error": str(e), "total": 0}
        last = max((r.get("captured_at", "") for r in rules), default="")
        return {
            "status": "ok",
            "total": len(rules),
            # [D1]: no status field upstream. Everything reachable here is
            # created; published-ness lives in deployments, not on the standard.
            "proposed": len(rules),
            "last_proposal": last or None,
        }
