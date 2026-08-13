"""Plain-English playbook rule -> ast-grep YAML, via LLM.

The bridge between Wedge 2 (rules as prose) and Wedge 4 (rules as executable
patterns). Not every rule survives the crossing: "prefer clear naming" has no
structural form, and the honest answer there is NOT_AUTOMATABLE rather than a
pattern that fires on everything.
"""

from pathlib import Path

from .reflector import complete  # single litellm entry point, CHRONOS_LLM_MODEL

RULES_DIR = Path(".chronos/rules")
NOT_AUTOMATABLE = "NOT_AUTOMATABLE"

PROMPT = """You are an expert at writing ast-grep structural search patterns.
Given this coding rule: '{rule_text}'
Write an ast-grep YAML rule that detects violations of this rule in {language} code.
The rule must:
- Have id: '{rule_id}'
- Have language: '{language}'
- Use rule.pattern (or rule.any/rule.all for complex cases)
- Have a clear message explaining the violation
- Have severity: warning (never error — Chronos controls blocking separately via OPA)
Return ONLY valid YAML. No explanation. If the rule cannot be expressed as an
ast-grep pattern, return the string 'NOT_AUTOMATABLE' with a one-line reason.{evidence}"""


def _strip_fence(text):
    """LLMs fence YAML even when told not to."""
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            for lang in ("yaml", "yml"):
                if t.lstrip().lower().startswith(lang):
                    t = t.lstrip()[len(lang):]
                    break
    return t.strip()


def generate(rule_text, language, rule_id, evidence_node=None) -> dict:
    """Generate an ast-grep rule. Always returns raw_llm_output for debugging."""
    evidence = (f"\nThis rule was learned from the symbol '{evidence_node}'; "
                "prefer a pattern that would match code involving it."
                if evidence_node else "")
    raw = complete(PROMPT.format(rule_text=rule_text, language=language,
                                 rule_id=rule_id, evidence=evidence))
    body = _strip_fence(raw)

    if NOT_AUTOMATABLE in body.upper():
        reason = body.upper().split(NOT_AUTOMATABLE, 1)[1].strip(" :-\n") or \
            "no structural form given"
        return {"rule_id": rule_id, "language": language, "yaml_pattern": None,
                "automatable": False, "not_automatable_reason": reason,
                "raw_llm_output": raw}

    RULES_DIR.mkdir(parents=True, exist_ok=True)
    (RULES_DIR / f"{rule_id}.yml").write_text(body, encoding="utf-8")
    return {"rule_id": rule_id, "language": language, "yaml_pattern": body,
            "automatable": True, "not_automatable_reason": None,
            "raw_llm_output": raw}
