"""callable_coverage: what fraction of callable symbols have a known caller.

WHY THIS EXISTS
---------------
Chronos answered `as_of_callers("checkConflicts")` with `count: 0` and
`no_data_reason: "existed but had no callers"` for a method with three live
production call sites. The graph was not lying about its own contents -- the
vendored indexer never produced a CALLS edge, because Setu is NestJS and the
call goes through `this.injectedService.method()`, which needs cross-file type
resolution the indexer does not do.

An agent cannot tell that answer apart from a correct one. This metric makes
the difference visible BEFORE a query is trusted: 8% coverage means "caller
queries here are unreliable", and doctor says so.

WHAT COUNTS, AND WHY THE NAIVE VERSION IS WRONG
-----------------------------------------------
Measured 2026-08-17, naive (every Method/Function) vs library-only:

    repo                naive   library-only   truth
    Opencode  (TS)       0.76       0.77        healthy
    litestar  (Python)   0.28       0.70        healthy -- naive was an artifact
    Setu      (NestJS)   0.17       0.08        genuinely broken

The naive form ranked litestar (a well-tested framework) as WORSE than Setu,
when it is ~9x better. Three exclusions fix it:

  * dunders  -- `__init__`/`__call__` are invoked by the interpreter. No source
    line calls them, so no CALLS edge can exist. Not a miss.
  * test functions and test/spec paths -- invoked by the runner, never
    explicitly. Penalising a repo for having tests is backwards.
  * non-library paths (docs/, examples/) -- samples, not call sites.

SQLITE TRAP: `LIKE '__%'` treats `_` as a single-character wildcard, so it
matches nearly every identifier and silently zeroes the denominator. Use
`substr(name,1,2) <> '__'`.

REMAINING KNOWN BIAS: public API of a library is called by user code that is
not in the repo, so a framework's true coverage is understated. This is why
the thresholds warn rather than fail, and why the message names DI as a
*probable* cause rather than asserting it.
"""

import json
import sqlite3
from pathlib import Path

WARN_BELOW = 0.50
ERROR_BELOW = 0.25

_CALLABLE = "label IN ('Method','Function')"

# Frameworks whose calls go through constructor-injected dependencies, which
# the indexer cannot resolve. Presence of these turns a low score from "unknown
# cause" into a named, actionable one.
_DI_MARKERS = (
    ("@Injectable", "NestJS/Angular dependency injection"),
    ("@Component", "Angular dependency injection"),
    ("@Autowired", "Spring dependency injection"),
    ("@Inject", "decorator-based dependency injection"),
    ("NestFactory", "NestJS"),
)


def _predicate(alias: str = "") -> str:
    """The 'is a library callable' clause, optionally column-qualified.

    Generated for a given alias instead of string-patching finished SQL, so the
    same definition serves both the denominator (bare `nodes`) and the
    numerator (`nodes AS t`) without either drifting from the other.
    """
    a = alias
    return (
        f"{a}label IN ('Method','Function') "
        f"AND substr({a}name,1,2) <> '__' "
        f"AND substr({a}name,1,5) <> 'test_' "
        f"AND {a}file_path NOT LIKE 'test/%' AND {a}file_path NOT LIKE 'tests/%' "
        f"AND {a}file_path NOT LIKE '%/test/%' AND {a}file_path NOT LIKE '%/tests/%' "
        f"AND {a}file_path NOT LIKE '%/__tests__/%' "
        f"AND {a}file_path NOT LIKE 'test_apps/%' "
        f"AND {a}file_path NOT LIKE '%.spec.%' AND {a}file_path NOT LIKE '%.test.%' "
        f"AND {a}file_path NOT LIKE '%_test.%' "
        f"AND {a}file_path NOT LIKE '%/docs/%' AND {a}file_path NOT LIKE 'docs/%' "
        f"AND {a}file_path <> '<python-builtins>'")


def _q(con, sql: str) -> int:
    try:
        r = con.execute(sql).fetchone()
        return int(r[0]) if r and r[0] is not None else 0
    except sqlite3.Error:
        return 0


def compute(upstream_db) -> dict:
    """callable_coverage for an upstream index. Read-only; safe on a live file."""
    p = Path(upstream_db)
    if not p.exists():
        return {"callable_coverage": None, "reason": "upstream index not found"}

    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        where = _predicate("")
        total = _q(con, f"SELECT count(*) FROM nodes WHERE {where}")
        if not total:
            return {"callable_coverage": None, "callable_total": 0,
                    "reason": "no library callables in index"}

        # Build the aliased form from the same source rather than string-
        # replacing an already-built clause: replacing "name"/"file_path" in
        # finished SQL also rewrites them inside string literals like 'test_'
        # and double-prefixes columns on a second pass.
        called = _q(con, f"""SELECT count(DISTINCT t.id) FROM edges e
                             JOIN nodes t ON e.target_id = t.id
                             WHERE e.type='CALLS' AND {_predicate('t.')}""")
        raw_total = _q(con, f"SELECT count(*) FROM nodes WHERE {_CALLABLE}")
        cov = round(called / total, 3)
        out = {
            "callable_coverage": cov,
            "callable_total": total,
            "callable_with_callers": called,
            "excluded_from_denominator": raw_total - total,
            "level": ("error" if cov < ERROR_BELOW
                      else "warn" if cov < WARN_BELOW else "ok"),
        }
        if cov < WARN_BELOW:
            out["probable_cause"] = _probable_cause(con)
        return out
    finally:
        con.close()


def _probable_cause(con) -> str:
    """Name a likely reason for low coverage, from evidence in the index."""
    for marker, label in _DI_MARKERS:
        try:
            n = con.execute(
                "SELECT count(*) FROM nodes WHERE name LIKE ? OR IFNULL(properties,'') LIKE ?",
                (f"%{marker}%", f"%{marker}%")).fetchone()[0]
        except sqlite3.Error:
            n = 0
        if n:
            return (f"{label} detected ({marker} x{n}). The indexer resolves "
                    f"this.method() but not this.injectedDep.method(), so callers "
                    f"through injected services are missing.")
    return ("dynamic dispatch, decorators, or re-exports the indexer cannot "
            "resolve statically. Verify caller queries with grep before relying "
            "on them.")


def manifest_path(upstream_db) -> Path:
    """Where the coverage manifest for an index lives (beside the index)."""
    return Path(upstream_db).with_suffix(".coverage.json")


def write_manifest(upstream_db, group_id: str, indexed_at: str) -> dict:
    """Compute and persist coverage. Called at index time, not query time.

    doctor reads this file rather than recomputing, so a diagnostic never pays
    for a full scan of the index -- and so the number reflects the index as it
    was built, not as it drifted.
    """
    data = compute(upstream_db)
    data.update({"group_id": group_id, "indexed_at": indexed_at,
                 "upstream_db": str(upstream_db)})
    try:
        manifest_path(upstream_db).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass  # a read-only cache dir must not fail the index
    return data


def read_manifest(upstream_db) -> dict | None:
    """The stored manifest, or None if this index predates coverage tracking."""
    try:
        return json.loads(manifest_path(upstream_db).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def warning_line(cov: dict | None) -> str | None:
    """One human-readable line for doctor, or None when coverage is fine."""
    if not cov or cov.get("callable_coverage") is None:
        return None
    c = cov["callable_coverage"]
    if c >= WARN_BELOW:
        return None
    sev = "ERROR" if c < ERROR_BELOW else "WARN"
    msg = (f"{sev} low call-graph coverage ({c:.0%}). Caller queries will "
           f"under-report -- as_of_callers may return 0 for symbols that do "
           f"have callers.")
    cause = cov.get("probable_cause")
    return f"{msg}\n              Likely cause: {cause}" if cause else msg
