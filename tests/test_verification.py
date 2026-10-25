"""Tests for AST rule lifecycle and deterministic enforcement gates."""

from chronos.verification.enforcer import evaluate_rules_on_content
from chronos.verification.rule_store import (
    upsert_rule,
    promote_rule,
    list_rules,
    get_active_blocking_rules,
    BLOCKING,
)

def test_rule_lifecycle(memory_db):
    upsert_rule(
        memory_db,
        rule_id="no-eval",
        language="python",
        rule_text="Do not use eval()",
        yaml_pattern=r"\beval\(",
        detectability_passed=True,
    )

    # Initial state is unvalidated/warn
    rules = list_rules(memory_db)
    assert len(rules) == 1
    assert rules[0]["status"] != BLOCKING

    # Not yet blocking
    assert len(get_active_blocking_rules(memory_db)) == 0

    # Human promotes rule to blocking
    promoted = promote_rule(memory_db, rule_id="no-eval", promoted_by="security_lead")
    assert promoted is True
    assert len(get_active_blocking_rules(memory_db)) == 1

def test_enforcement_gate_blocks_violations(memory_db):
    upsert_rule(
        memory_db,
        rule_id="ban-eval",
        language="python",
        rule_text="eval is forbidden",
        yaml_pattern=r"\beval\s*\(",
        detectability_passed=True,
    )
    promote_rule(memory_db, "ban-eval", promoted_by="sec_admin")

    # Clean code passes
    clean_code = "def parse_data(x): return json.loads(x)"
    passed1, violations1 = evaluate_rules_on_content(memory_db, clean_code, "parse.py")
    assert passed1 is True
    assert len(violations1) == 0

    # Code with eval() is blocked
    bad_code = "def exec_input(user_str): eval(user_str)"
    passed2, violations2 = evaluate_rules_on_content(memory_db, bad_code, "exec.py")
    assert passed2 is False
    assert len(violations2) == 1
    assert violations2[0]["rule_id"] == "ban-eval"
