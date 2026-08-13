"""Curator — decides whether a candidate rule earns a place in the playbook.

Dedup first (cheap, objective), quality gate second (LLM judgement). A rule that
survives both is created in Packmind as an UNPUBLISHED standard: see playbook.py
[D2] — not publishing IS the human-approval gate, since only a published standard
reaches CLAUDE.md/.cursor/rules.
"""

import math
import os

from .playbook import Packmind
from .reflector import _json, complete

SIMILARITY_LIMIT = 0.85
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


def curate(candidate, packmind=None):
    """CandidateRule -> {submitted, reason, packmind_proposal_id}.

    PackmindError is deliberately NOT caught: an unreachable store must fail
    loudly rather than silently drop a lesson (PRD Step 4)."""
    pm = packmind or Packmind()

    existing = pm.list_rules()
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
    sid = pm.create_standard(candidate["rule_text"], evidence)
    return {
        "submitted": True,
        "reason": "created in Packmind as an unpublished standard, awaiting human publish",
        "packmind_proposal_id": sid,
    }
