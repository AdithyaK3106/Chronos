"""Seed the 4 NovaPay demo rules into chronos.db. Run once against the demo group.

ponytail: one-shot seed script, not a management command. Re-run is idempotent
(upsert_rule/promote_to_blocking are both upsert-safe).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chronos import rule_store

RULES = [
    dict(
        rule_id="no-requests",
        language="python",
        rule_text="Don't use the requests library -- use httpx (async).",
        yaml_pattern=(
            "id: no-requests\n"
            "language: python\n"
            "severity: error\n"
            "message: use httpx.AsyncClient instead of requests -- see docs/migration-guide.md\n"
            "rule:\n"
            "  any:\n"
            "    - pattern: requests.post($$$ARGS)\n"
            "    - pattern: requests.get($$$ARGS)\n"
            "  inside:\n"
            "    kind: function_definition\n"
            "    stopBy: end\n"
            "    has:\n"
            "      field: name\n"
            "      pattern: $NAME\n"
        ),
        state="blocking",
    ),
    dict(
        rule_id="no-raw-sql",
        language="python",
        rule_text="Don't write raw SQL -- use the SQLAlchemy ORM.",
        yaml_pattern=(
            "id: no-raw-sql\n"
            "language: python\n"
            "severity: warning\n"
            "message: write queries through the SQLAlchemy ORM instead of raw cursor.execute\n"
            "rule:\n"
            "  pattern: $CUR.execute($$$ARGS)\n"
        ),
        state="warn-only",
    ),
    dict(
        rule_id="require-type-hints",
        language="python",
        rule_text="New functions must have type hints on their parameters.",
        yaml_pattern=(
            "id: require-type-hints\n"
            "language: python\n"
            "severity: warning\n"
            "message: add type hints to new function parameters\n"
            "rule:\n"
            "  kind: function_definition\n"
            "  has:\n"
            "    field: parameters\n"
            "    has:\n"
            "      kind: identifier\n"
        ),
        state="warn-only",
    ),
    dict(
        rule_id="no-direct-db-in-routes",
        language="python",
        rule_text="Routes must not import db models/repositories directly.",
        yaml_pattern=(
            "id: no-direct-db-in-routes\n"
            "language: python\n"
            "severity: error\n"
            "message: routes must go through a service layer, not import repository/db modules directly\n"
            "rule:\n"
            "  any:\n"
            "    - pattern: from src.db import $$$X\n"
            "    - pattern: from src.$MOD.repository import $$$X\n"
        ),
        state="blocking",
    ),
]


def main():
    con = rule_store.connect()
    for r in RULES:
        rule_store.upsert_rule(
            r["rule_id"], r["language"], r["rule_text"], r["yaml_pattern"],
            detectability_result={"passed": True, "false_positive_risk": False},
            con=con,
        )
        rule_store.approve_rule(r["rule_id"], con=con)
        if r["state"] == "blocking":
            rule_store.promote_to_blocking(r["rule_id"], promoted_by="demo-seed", con=con)
    con.commit()
    for r in rule_store.get_all_rules(con=con):
        print(r["rule_id"], r["status"])


if __name__ == "__main__":
    main()
