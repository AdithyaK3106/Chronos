"""Rule lifecycle: generated -> validated -> (human) promoted to blocking.

Lives in the ledger SQLite DB, not the graph: these are operational records
with the same lifetime as provenance events, and enforcement already needs a
ledger connection to stamp blocks.
"""

import sqlite3
from datetime import datetime, timezone

from . import ledger

SCHEMA = """
CREATE TABLE IF NOT EXISTS enforcement_rules (
    rule_id              TEXT PRIMARY KEY,
    language             TEXT NOT NULL,
    rule_text            TEXT NOT NULL DEFAULT '',
    yaml_pattern         TEXT,
    status               TEXT NOT NULL,
    detectability_passed INTEGER NOT NULL DEFAULT 0,
    false_positive_risk  INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    promoted_at          TEXT,
    promoted_by          TEXT
);
"""

UNVALIDATED = "warn-only-unvalidated"
VALIDATED = "warn-only-validated"
BLOCKING = "blocking"


def connect(path=None) -> sqlite3.Connection:
    con = ledger.connect(path)
    con.executescript(SCHEMA)
    con.commit()
    return con


def _now():
    return datetime.now(timezone.utc).isoformat()


def _use(con):
    """Caller-supplied connection, or a short-lived one we own."""
    return (con, False) if con is not None else (connect(), True)


def upsert_rule(rule_id, language, rule_text, yaml_pattern, detectability_result,
                con=None) -> None:
    """Insert or update a rule. Never sets 'blocking' -- promotion is a separate,
    explicit, human act (chronos_promote_rule)."""
    d = detectability_result or {}
    passed = bool(d.get("passed"))
    status = VALIDATED if passed else UNVALIDATED
    c, own = _use(con)
    try:
        c.execute("""
            INSERT INTO enforcement_rules
                (rule_id, language, rule_text, yaml_pattern, status,
                 detectability_passed, false_positive_risk, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(rule_id) DO UPDATE SET
                language=excluded.language,
                rule_text=excluded.rule_text,
                yaml_pattern=excluded.yaml_pattern,
                detectability_passed=excluded.detectability_passed,
                false_positive_risk=excluded.false_positive_risk,
                -- a rule already promoted to blocking stays blocking on
                -- regeneration; demoting silently would disable a check a
                -- human deliberately turned on.
                status=CASE WHEN enforcement_rules.status='blocking'
                            THEN 'blocking' ELSE excluded.status END
        """, (rule_id, language, rule_text or "", yaml_pattern, status,
              int(passed), int(bool(d.get("false_positive_risk"))), _now()))
        if own:
            c.commit()
    finally:
        if own:
            c.close()


def promote_to_blocking(rule_id, promoted_by, con=None) -> dict:
    """Promote a validated rule. Refuses unvalidated ones -- a rule that cannot
    catch its own example must never gate a merge."""
    c, own = _use(con)
    try:
        row = c.execute("SELECT * FROM enforcement_rules WHERE rule_id=?",
                        (rule_id,)).fetchone()
        if row is None:
            return {"rule_id": rule_id, "promoted": False,
                    "reason": "no such rule", "status": None}
        if not row["detectability_passed"]:
            return {"rule_id": rule_id, "promoted": False,
                    "reason": "detectability did not pass — cannot promote an "
                              "unvalidated rule to blocking",
                    "status": row["status"]}
        ts = _now()
        c.execute("UPDATE enforcement_rules SET status=?, promoted_at=?, promoted_by=? "
                  "WHERE rule_id=?", (BLOCKING, ts, promoted_by, rule_id))
        if own:
            c.commit()
        return {"rule_id": rule_id, "promoted": True, "status": BLOCKING,
                "promoted_at": ts, "promoted_by": promoted_by}
    finally:
        if own:
            c.close()


def get_active_rules(language=None, con=None) -> list[dict]:
    c, own = _use(con)
    try:
        q = "SELECT * FROM enforcement_rules WHERE yaml_pattern IS NOT NULL"
        args = ()
        if language:
            q += " AND language=?"
            args = (language,)
        return [dict(r) for r in c.execute(q + " ORDER BY rule_id", args).fetchall()]
    finally:
        if own:
            c.close()


def get_rule(rule_id, con=None) -> dict | None:
    c, own = _use(con)
    try:
        r = c.execute("SELECT * FROM enforcement_rules WHERE rule_id=?",
                      (rule_id,)).fetchone()
        return dict(r) if r else None
    finally:
        if own:
            c.close()


def counts(con=None) -> dict:
    c, own = _use(con)
    try:
        rows = c.execute("SELECT status, count(*) n FROM enforcement_rules "
                         "GROUP BY status").fetchall()
        by = {r["status"]: r["n"] for r in rows}
        return {"total": sum(by.values()),
                "blocking": by.get(BLOCKING, 0),
                "warn_only": by.get(VALIDATED, 0) + by.get(UNVALIDATED, 0)}
    finally:
        if own:
            c.close()
