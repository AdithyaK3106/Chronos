"""Rule persistence and lifecycle management (Wedge 4).

Lifecycle states:
- 'proposed': draft rule; not enforced.
- 'warn-only-unvalidated': warns on matches; unverified.
- 'warn-only-validated': verified against detectability tests; warns only.
- 'blocking': promoted by human or policy gate; strictly blocks commits/tool calls.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any

PROPOSED = "proposed"
WARN_UNVALIDATED = "warn-only-unvalidated"
WARN_VALIDATED = "warn-only-validated"
BLOCKING = "blocking"

def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def upsert_rule(
    con: sqlite3.Connection,
    rule_id: str,
    language: str,
    rule_text: str,
    yaml_pattern: str | None = None,
    detectability_passed: bool = False,
    status: str = WARN_UNVALIDATED,
) -> None:
    now = _utc_now_iso()
    with con:
        con.execute(
            """
            INSERT INTO enforcement_rules (rule_id, language, rule_text, yaml_pattern, status, detectability_passed, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                language=excluded.language,
                rule_text=excluded.rule_text,
                yaml_pattern=excluded.yaml_pattern,
                status=excluded.status,
                detectability_passed=excluded.detectability_passed
            """,
            (rule_id, language, rule_text, yaml_pattern, status, 1 if detectability_passed else 0, now),
        )

def promote_rule(con: sqlite3.Connection, rule_id: str, promoted_by: str) -> bool:
    """Promote a rule to 'blocking' status."""
    now = _utc_now_iso()
    with con:
        cur = con.execute(
            "UPDATE enforcement_rules SET status = ?, promoted_at = ?, promoted_by = ? WHERE rule_id = ?",
            (BLOCKING, now, promoted_by, rule_id),
        )
        return cur.rowcount > 0

def list_rules(con: sqlite3.Connection, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        rows = con.execute("SELECT * FROM enforcement_rules WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM enforcement_rules ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]

def get_active_blocking_rules(con: sqlite3.Connection) -> list[dict[str, Any]]:
    return list_rules(con, status=BLOCKING)
