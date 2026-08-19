"""Curator — decides whether a candidate rule earns a place in the playbook.

Dedup first (cheap, objective), quality gate second (LLM judgement). A rule that
survives both is created in Packmind as an UNPUBLISHED standard: see playbook.py
[D2] — not publishing IS the human-approval gate, since only a published standard
reaches CLAUDE.md/.cursor/rules.
"""

import math
import os

from .playbook import Packmind, PackmindNotConfigured
from .reflector import _json, complete

SIMILARITY_LIMIT = 0.85


def _get_submission_path() -> str:
    """Returns 'packmind' if PACKMIND_API_URL is set, else 'git-native'."""
    return "packmind" if os.environ.get("PACKMIND_API_URL") else "git-native"
EMBED_MODEL = lambda: os.environ.get("CHRONOS_EMBED_MODEL", "text-embedding-3-small")

GATE_PROMPT = """Assess this candidate coding standard rule before it enters a team playbook.

RULE
{rule}

EVIDENCE
node: {node}
last changed: {valid_at}
history: {context}
extracted from a real agent failure with confidence {confidence}

Assess:
(a) Is it specific enough to be actionable? (not "write good code")
(b) Is it grounded in a real failure, not a generic best practice?
(c) Would it cause false positives if applied broadly?

Respond with ONLY: {{"passes_gate": true/false, "reason": "one sentence"}}"""


def embed(texts, model=None):
    """Embeddings via litellm, same configurability as completions."""
    import litellm

    r = litellm.embedding(model=model or EMBED_MODEL(), input=texts)
    return [d["embedding"] for d in r["data"]]


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _duplicate_of(rule_text, existing):
    """Nearest existing rule above the similarity limit, else None."""
    texts = [e.get("rule_text", "") for e in existing if e.get("rule_text")]
    if not texts:
        return None
    vectors = embed([rule_text] + texts)
    candidate, rest = vectors[0], vectors[1:]
    best, score = None, 0.0
    for existing_rule, vec in zip([e for e in existing if e.get("rule_text")], rest):
        s = cosine(candidate, vec)
        if s > score:
            best, score = existing_rule, s
    return (best, score) if score > SIMILARITY_LIMIT else None


def _existing_rules(pm):
    """Rules for the dedup check, from whichever store is active.

    Git-native: get_all_rules(), not get_active_rules() — a rule sits at
    'proposed' until its PR is merged, so restricting to active rules made
    dedup blind to every proposal still awaiting review (the common case)."""
    if pm is not None:
        return pm.list_rules()
    from . import rule_store
    return [{"name": r["rule_id"], "rule_text": r["rule_text"]}
            for r in rule_store.get_all_rules()
            if r.get("rule_text")]


def curate(candidate, packmind=None, repo_path=None):
    """CandidateRule -> {submitted, reason, packmind_proposal_id}.

    Routes on PACKMIND_API_URL: set -> Packmind HTTP, unset -> git-native PR.
    A genuinely unreachable Packmind still raises PackmindError — only the
    narrower 'not configured' case falls through to git-native, since an
    outage must stay loud (PRD Step 4)."""
    pm = packmind
    if pm is None and _get_submission_path() == "packmind":
        try:
            pm = Packmind()
        except PackmindNotConfigured:
            pm = None  # env vanished between check and construction

    existing = _existing_rules(pm)
    dup = _duplicate_of(candidate["rule_text"], existing)
    if dup:
        rule, score = dup
        return {
            "submitted": False,
            "reason": f"duplicate of existing rule '{rule.get('name')}' (similarity {score:.2f})",
            "packmind_proposal_id": None,
        }

    verdict = _json(complete(GATE_PROMPT.format(
        rule=candidate["rule_text"],
        node=candidate.get("evidence_node") or "(none)",
        valid_at=candidate.get("evidence_valid_at") or "(unknown)",
        context=candidate.get("evidence_commit_context") or "(none)",
        confidence=candidate.get("confidence"),
    )))
    if not verdict.get("passes_gate"):
        return {
            "submitted": False,
            "reason": f"quality gate: {verdict.get('reason', 'no reason given')}",
            "packmind_proposal_id": None,
        }

    evidence = {
        "evidence_node": candidate.get("evidence_node"),
        "evidence_valid_at": candidate.get("evidence_valid_at"),
        "evidence_commit_context": candidate.get("evidence_commit_context"),
        "source": "chronos-wedge2",
        "source_trace_id": candidate.get("source_trace_id"),
        "agent_id": candidate.get("agent_id"),
        "confidence": candidate.get("confidence"),
        "captured_at": candidate.get("captured_at"),
        # [D1] Packmind has no status field. Recorded here so the intent is
        # visible in the UI; the real gate is that we never publish.
        "status": "proposed",
    }
    if pm is not None:
        try:
            sid = pm.create_standard(candidate["rule_text"], evidence)
            return {
                "submitted": True,
                "reason": "created in Packmind as an unpublished standard, "
                          "awaiting human publish",
                "packmind_proposal_id": sid,
                "submission_path": "packmind",
            }
        except PackmindNotConfigured:
            pass  # fall through: degrade cleanly rather than drop the lesson

    from .rule_submission import submit_git_native
    r = submit_git_native({**candidate, **{"evidence": evidence}}, repo_path)
    return {
        "submitted": True,
        "reason": ("proposed as a draft PR, awaiting merge" if r["pr_url"]
                   else "written to .chronos/rules/, awaiting review"),
        "packmind_proposal_id": None,
        "submission_path": "git-native",
        "rule_id": r["rule_id"],
        "branch": r["branch"],
        "pr_url": r["pr_url"],
        "rule_path": r["rule_path"],
    }
