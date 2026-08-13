"""Validate a generated pattern before it can ever gate a merge.

A rule that misfires at scale burns partner trust irreversibly, and a rule that
matches nothing is worse than none at all -- it looks like coverage. Both checks
here are cheap and run before storage.

CHECK A  syntax: does ast-grep accept the YAML? (exit 8 == it does not; see
         enforcer.py [A4] -- matches never change the exit code, so this is a
         clean signal)
CHECK B  self-match: the LLM writes a snippet the rule SHOULD catch; if the rule
         does not catch its own example, it is broken regardless of syntax.
         A negative example is also generated -- firing on it flags false-positive
         risk, which is recorded but never blocks storage.
"""

import os
import tempfile
from pathlib import Path

from .enforcer import EXIT_RULE_PARSE, scan
from .reflector import complete

EXT = {"typescript": ".ts", "tsx": ".tsx", "javascript": ".js", "python": ".py",
       "go": ".go", "rust": ".rs", "java": ".java", "c": ".c", "cpp": ".cpp",
       "ruby": ".rb", "csharp": ".cs", "kotlin": ".kt"}

POSITIVE = """Given this ast-grep rule:
{yaml}

Write a minimal {language} code snippet (5-15 lines) that this rule SHOULD match.
Return only the code, no explanation."""

NEGATIVE = """Given this ast-grep rule:
{yaml}

Write a minimal {language} code snippet (5-15 lines) that this rule should NOT
match -- similar code that is correct and compliant.
Return only the code, no explanation."""


def _strip_fence(text):
    t = (text or "").strip()
    if t.startswith("```"):
        parts = t.split("```")
        if len(parts) >= 2:
            t = parts[1]
            if "\n" in t:  # drop a leading language tag
                first, rest = t.split("\n", 1)
                if first.strip().isalpha():
                    t = rest
    return t.strip()


def _tmp(content, suffix):
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def validate(rule_id, yaml_pattern, language) -> dict:
    """Run both checks. Never raises on a bad rule -- a failure is a result."""
    out = {"rule_id": rule_id, "syntax_valid": False,
           "catches_true_positive": False, "false_positive_risk": False,
           "passed": False, "details": ""}
    ext = EXT.get(language.lower(), ".txt")
    rule_file = _tmp(yaml_pattern or "", ".yml")

    try:
        # --- CHECK A: syntax -------------------------------------------------
        probe = _tmp("", ext)
        try:
            _, code, err = scan(rule_file, probe)
        finally:
            Path(probe).unlink(missing_ok=True)
        if code == EXIT_RULE_PARSE:
            out["details"] = f"CHECK A failed: ast-grep cannot parse the rule — {err[:300]}"
            return out
        out["syntax_valid"] = True

        # --- CHECK B: self-match --------------------------------------------
        pos = _strip_fence(complete(POSITIVE.format(yaml=yaml_pattern, language=language)))
        if not pos:
            out["details"] = "CHECK B failed: no positive example generated"
            return out
        pos_file = _tmp(pos, ext)
        try:
            matches, code, err = scan(rule_file, pos_file)
        finally:
            Path(pos_file).unlink(missing_ok=True)
        if not matches:
            out["details"] = ("CHECK B failed: the rule does not match its own "
                              f"positive example ({err[:200] or 'zero matches'})")
            return out
        out["catches_true_positive"] = True

        # Negative example: informational, never fatal.
        neg = _strip_fence(complete(NEGATIVE.format(yaml=yaml_pattern, language=language)))
        if neg:
            neg_file = _tmp(neg, ext)
            try:
                nmatches, _, _ = scan(rule_file, neg_file)
            finally:
                Path(neg_file).unlink(missing_ok=True)
            if nmatches:
                out["false_positive_risk"] = True

        out["passed"] = True
        out["details"] = (f"matched {len(matches)} in the positive example"
                          + ("; ALSO fired on the negative example — high "
                             "false-positive risk" if out["false_positive_risk"] else ""))
        return out
    finally:
        Path(rule_file).unlink(missing_ok=True)
