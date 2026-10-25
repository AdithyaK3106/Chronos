"""Deterministic AST rule enforcement (Wedge 4)."""

import re
import sqlite3
from typing import Any

from chronos.verification.rule_store import get_active_blocking_rules

def evaluate_rules_on_content(
    con: sqlite3.Connection,
    content: str,
    file_path: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Check content against all active blocking rules.
    
    Returns:
        (passed, violations)
    """
    blocking_rules = get_active_blocking_rules(con)
    violations = []

    for rule in blocking_rules:
        pattern = rule.get("yaml_pattern") or rule.get("rule_text")
        if not pattern:
            continue

        # Simple regex/substring pattern match fallback for zero-dependency portability
        try:
            if re.search(pattern, content):
                violations.append({
                    "rule_id": rule["rule_id"],
                    "message": f"Violation of blocking rule '{rule['rule_id']}': pattern '{pattern}' matched in {file_path}",
                    "file": file_path,
                })
        except re.error:
            # Literal substring check if regex compilation fails
            if pattern in content:
                violations.append({
                    "rule_id": rule["rule_id"],
                    "message": f"Violation of blocking rule '{rule['rule_id']}': literal '{pattern}' matched in {file_path}",
                    "file": file_path,
                })

    passed = len(violations) == 0
    return passed, violations
