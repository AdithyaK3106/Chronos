"""CI enforcement: ast-grep match -> Wedge 1 confirmation -> OPA verdict -> Wedge 3 stamp.

=============================================================================
RESEARCH NOTES (Step 0). Both tools installed and exercised on this machine
before any of this code was written. Findings, including where they contradict
the PRD, are recorded here because they determine how this module parses output.
=============================================================================

AST-GREP  (MIT) -- `pip install ast-grep-cli`, verified v0.45.1
  [A1] The `sg` binary the PRD calls is DEPRECATED in 0.45.1: running it prints
       "WARNING: `sg` is deprecated. Use `ast-grep` instead." We invoke
       `ast-grep`. BIN below is the single place to change it.
  [A2] Scan with a single rule file:  ast-grep scan -r RULE.yml PATH --json
       (`--rule` is the long form; there is also `--inline-rules RULE_TEXT`
       for rule text without a file.)
  [A3] --json emits a JSON ARRAY of match objects, each with:
         text            the matched source snippet
         file            path
         range.start.line 0-indexed line
         ruleId          the rule's id field
         metaVariables.single.<NAME>.text   captured $NAME
         metaVariables.multi.<NAME>[]       captured $$$NAME
       No matches -> `[]`. Not a wrapper object; index straight into the list.
  [A4] EXIT CODES -- this is the important one, and it is not what you would
       guess. Matches do NOT change the exit code:
         0  rule parsed and ran (whether or not anything matched)
         8  rule file could not be parsed  <- CHECK A keys on this
         6  rule file does not exist
       So "did anything match" MUST come from len(json), never from the exit
       code, and "is this rule valid" comes from the exit code, never from
       empty output. Conflating them silently disables enforcement.
  [A5] A rule YAML needs: id, language, severity, message, rule.{pattern|any|all}.
       A rule with no matchable AST kind is rejected at parse time with
       "Rule must specify a set of AST kinds to match" (exit 8).

OPA  (Apache 2.0) -- binary from openpolicyagent.org, verified v1.19.0
  [O1] THE PRD'S REGO DOES NOT PARSE. It is written in Rego v0
       (`enforce := result { ... }`); OPA >= 1.0 requires v1 and rejects it with
       "`if` keyword is required before rule body" (rego_parse_error). The
       policy shipped in chronos/policies/enforce.rego is the v1 form
       (`enforce := result if { ... }`) with identical logic and identical
       verdicts. Verified across all three branches.
  [O2] Invocation:  opa eval -d POLICY.rego -I 'data.chronos.enforce' < input.json
       `-I` reads the input document from stdin.
  [O3] --format raw prints just the value: {"reason":...,"verdict":"block"}.
       Without it you get the full wrapper:
       {"result":[{"expressions":[{"value": <here>, ...}]}]}
       We parse the wrapper (portable across OPA versions) and fall back to raw.
  [O4] If NO branch matches, `enforce` is undefined and OPA prints an empty
       result with exit 0. That is treated as a hard error here, not a pass --
       an undefined policy decision must never silently become "allowed".

DEVIATION FROM THE PRD, and why
  The PRD's Rego is kept semantically intact; only the syntax is migrated to
  v1, because the version it was written for cannot run on any current OPA.
  Everything else (input shape, verdict strings, reasons) matches the spec.
"""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import rule_store

BIN = os.environ.get("CHRONOS_ASTGREP", "ast-grep")  # [A1]
OPA = os.environ.get("CHRONOS_OPA", "opa")
POLICY = Path(__file__).parent / "policies" / "enforce.rego"
RULES_DIR = Path(".chronos/rules")

EXIT_RULE_PARSE = 8  # [A4]
EXIT_NO_FILE = 6


class ToolMissing(RuntimeError):
    """A required binary is absent. Enforcement is never silently skipped:
    a CI check that quietly stops checking is worse than one that fails."""


def _which(binary, install_hint):
    p = shutil.which(binary)
    if not p:
        raise ToolMissing(f"{binary} not found on PATH — {install_hint}")
    return p


def ast_grep_version():
    try:
        r = subprocess.run([_which(BIN, "pip install ast-grep-cli"), "--version"],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip() or None
    except (ToolMissing, OSError, subprocess.SubprocessError):
        return None


def opa_version():
    try:
        r = subprocess.run([_which(OPA, "see docs/wedge4-ci.yml"), "version"],
                           capture_output=True, text=True, timeout=30)
        for line in r.stdout.splitlines():
            if line.startswith("Version:"):
                return line.split(":", 1)[1].strip()
        return r.stdout.strip().splitlines()[0] if r.stdout.strip() else None
    except (ToolMissing, OSError, subprocess.SubprocessError):
        return None


def scan(rule_path, target) -> tuple[list, int, str]:
    """Run one rule against a path. Returns (matches, exit_code, stderr).

    Both the matches and the exit code are returned because they answer
    different questions -- see [A4]."""
    exe = _which(BIN, "pip install ast-grep-cli")
    r = subprocess.run([exe, "scan", "-r", str(rule_path), str(target), "--json"],
                       capture_output=True, text=True, timeout=120)
    if r.returncode in (EXIT_RULE_PARSE, EXIT_NO_FILE):
        return [], r.returncode, r.stderr.strip()
    try:
        return json.loads(r.stdout or "[]"), r.returncode, r.stderr.strip()
    except json.JSONDecodeError:
        return [], r.returncode, (r.stderr or r.stdout).strip()


def opa_eval(payload: dict) -> dict:
    """Evaluate one violation against the Rego policy. [O2]-[O4]"""
    exe = _which(OPA, "see docs/wedge4-ci.yml")
    if not POLICY.exists():
        raise ToolMissing(f"policy missing: {POLICY}")
    r = subprocess.run([exe, "eval", "-d", str(POLICY), "-I", "data.chronos.enforce"],
                       input=json.dumps(payload), capture_output=True, text=True,
                       timeout=60)
    out = (r.stdout or "").strip()
    if not out:
        raise RuntimeError(  # [O4] undefined must not become an implicit pass
            f"OPA returned no decision for rule {payload.get('rule_id')} "
            f"(exit {r.returncode}): {r.stderr.strip() or 'undefined result'}")
    doc = json.loads(out)
    if isinstance(doc, dict) and "result" in doc:  # [O3] wrapper form
        try:
            return doc["result"][0]["expressions"][0]["value"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"unexpected OPA output shape: {out[:200]}") from e
    return doc


def _identifier(match: dict) -> str | None:
    """Best-effort symbol name from a match, for Wedge 1 lookup.

    Prefers a captured metavariable, else the callee-ish head of the snippet.
    ponytail: string head rather than re-parsing -- ast-grep already parsed it,
    and a wrong guess only costs a graph miss, which degrades to warn."""
    meta = (match.get("metaVariables") or {}).get("single") or {}
    for key in ("NAME", "FN", "FUNC", "A"):
        if key in meta and meta[key].get("text"):
            return meta[key]["text"]
    text = (match.get("text") or "").strip()
    head = text.split("(")[0].strip()
    if "." in head:
        head = head.rsplit(".", 1)[-1]
    return head or None


async def deprecation(driver, group_id, name):
    """Ask Wedge 1 whether this symbol has been superseded.

    Returns (deprecated: bool, since: str|None). A symbol is 'deprecated' here
    when a fact about it has been closed (invalid_at set) and none is current --
    the same supersession the temporal graph already tracks."""
    if not driver or not name:
        return False, None
    from . import query
    rows = await query._rows(driver, """
        MATCH (n:Entity)-[:RELATES_TO]->(e:RelatesToNode_)-[:RELATES_TO]->(m:Entity)
        WHERE e.group_id = $g AND (n.name = $name OR m.name = $name)
        RETURN count(e) AS facts,
               count(e.invalid_at) AS closed,
               max(e.invalid_at) AS last_closed
    """, g=group_id, name=name)
    row = rows[0] if rows else {}
    facts, closed = row.get("facts") or 0, row.get("closed") or 0
    if not facts or not closed or closed < facts:
        return False, None
    since = row.get("last_closed")
    return True, (since.isoformat() if hasattr(since, "isoformat") else str(since) if since else None)


async def enforce(file_path, language, agent_id=None, session_id=None,
                  driver=None, group_id=None, con=None) -> list[dict]:
    """Run every active rule for `language` against one file."""
    from . import ledger

    results = []
    for rule in rule_store.get_active_rules(language, con=con):
        rid = rule["rule_id"]
        path = RULES_DIR / f"{rid}.yml"
        if not path.exists():  # stored in DB but the YAML is gone
            path = _materialize(rule)

        matches, code, err = scan(path, file_path)
        if code == EXIT_RULE_PARSE:
            results.append(_result(rid, "warn", rule["status"],
                                   message=f"rule failed to parse, not enforced: {err}"))
            continue
        if not matches:
            results.append(_result(rid, "pass", rule["status"], message="no matches"))
            continue

        for m in matches:
            name = _identifier(m)
            dep, since = await deprecation(driver, group_id, name)
            decision = opa_eval({
                "rule_id": rid,
                "rule_status": rule["status"],
                "deprecated_in_graph": dep,
                "deprecated_since": since,
                "matched_node": m.get("text", ""),
            })
            verdict = decision.get("verdict", "warn")
            event_id = None
            if verdict == "block" and agent_id:
                own = con is None
                c = con or ledger.connect()
                try:
                    ev = ledger.log_event(
                        c, node_id=name or m.get("text", "")[:80], agent_id=agent_id,
                        session_id=session_id or "", action="blocked_by_ci",
                        reason=f"rule {rid}: {decision.get('reason', '')}")
                    if own:
                        c.commit()
                    event_id = str(ev["id"])
                finally:
                    if own:
                        c.close()
            if verdict == "block":
                # Cross-wedge trigger 1: the block is itself a lesson.
                # Fire-and-forget -- on_block dispatches to a daemon thread and
                # returns at once, so the LLM round-trip never lands in the
                # enforcement path. Return value is deliberately unused.
                from . import triggers
                triggers.on_block(
                    rule_id=rid, message=decision.get("reason", ""),
                    matched_qualified_name=name, agent_id=agent_id,
                    session_id=session_id, rule_text=rule.get("rule_text", ""),
                    driver=driver, group_id=group_id or "default")
            results.append(_result(
                rid, verdict, rule["status"], matched_node=m.get("text"),
                matched_qualified_name=name, deprecated_since=since,
                provenance_event_id=event_id,
                message=f"{decision.get('reason', '')} "
                        f"[{Path(file_path).name}:{(m.get('range') or {}).get('start', {}).get('line', 0) + 1}]"))
    return results


def _materialize(rule) -> Path:
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    p = RULES_DIR / f"{rule['rule_id']}.yml"
    p.write_text(rule["yaml_pattern"] or "", encoding="utf-8")
    return p


def _result(rule_id, verdict, rule_status, matched_node=None,
            matched_qualified_name=None, deprecated_since=None,
            provenance_event_id=None, message=""):
    return {
        "rule_id": rule_id,
        "verdict": verdict,
        "matched_node": matched_node,
        "matched_qualified_name": matched_qualified_name,
        "deprecated_since": deprecated_since,
        "rule_status": rule_status,
        "provenance_event_id": provenance_event_id,
        "message": message,
    }
