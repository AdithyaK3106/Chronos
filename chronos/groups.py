"""Group ownership: one repo per group_id, enforced at ingestion.

THE BUG THIS EXISTS TO PREVENT
------------------------------
`--group` and CHRONOS_GROUP_ID both defaulted to the literal string "default".
Every repo indexed without an explicit group therefore wrote into one shared
namespace. Because sync invalidates any fact in the group that is absent from
the new input, indexing repo B marked repo A's entire graph superseded.

Observed 2026-08-17: syncing Setu into `default` invalidated 1,212 facts and
reported Chronos's own internals (`ChronosDaemon`, `_checkout_branch`) as
"deprecated". Two unrelated codebases had merged into a single temporal
history, and every as-of answer drawn from it was silently wrong.

THE RULE: a group belongs to exactly one repo. First writer claims it; a
second repo is refused, not merged. Deriving the group from the repo path
makes the collision impossible by default, and the ownership table catches
the case where someone passes --group explicitly anyway.

Reject-and-log, never silently merge: a refused ingestion writes a SKIP row
to index_log with the reason, and `chronos doctor` reads that table -- a
rejection nobody can see is the same failure in a quieter costume.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import db as _db
from .upstream import repo_slug

SCHEMA = """
CREATE TABLE IF NOT EXISTS group_ownership (
    group_id   TEXT PRIMARY KEY,
    repo_path  TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    group_id  TEXT NOT NULL,
    repo_path TEXT NOT NULL DEFAULT '',
    outcome   TEXT NOT NULL,
    reason    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_indexlog_ts ON index_log(id DESC);
"""


class GroupConflict(RuntimeError):
    """Another repo already owns this group. Raised instead of merging."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _con(con=None):
    c = con or _db.get_db()
    c.executescript(SCHEMA)
    return c


def derive(repo: str | Path) -> str:
    """Group id for a repo path: its slug, lowercased.

    Deterministic, so re-indexing the same repo always lands in the same group
    and never collides with a different one. Reuses upstream's slug scheme so
    the group and the upstream cache filename stay recognisably paired.
    """
    return repo_slug(repo).lower()


def log(group_id: str, outcome: str, reason: str = "", repo: str = "", con=None) -> None:
    """Append to index_log. Queryable by doctor -- see module docstring."""
    c = _con(con)
    c.execute("INSERT INTO index_log (ts, group_id, repo_path, outcome, reason) "
              "VALUES (?,?,?,?,?)", (_now(), group_id, str(repo), outcome, reason))


def recent(limit: int = 20, con=None) -> list[dict]:
    """Most recent index_log rows, newest first."""
    c = _con(con)
    rows = c.execute("SELECT ts, group_id, repo_path, outcome, reason FROM index_log "
                     "ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def owner(group_id: str, con=None) -> str | None:
    """Repo path that owns this group, or None if unclaimed."""
    c = _con(con)
    r = c.execute("SELECT repo_path FROM group_ownership WHERE group_id=?",
                  (group_id,)).fetchone()
    return r["repo_path"] if r else None


def claim(group_id: str, repo: str | Path, con=None) -> str:
    """Assert that `repo` owns `group_id`. Returns the group id.

    Raises GroupConflict if a different repo already owns it -- the caller must
    not write. Same repo re-indexing is a no-op, which is the common path.
    """
    c = _con(con)
    resolved = str(Path(repo).resolve())
    held = owner(group_id, c)

    if held is None:
        c.execute("INSERT OR REPLACE INTO group_ownership (group_id, repo_path, claimed_at) "
                  "VALUES (?,?,?)", (group_id, resolved, _now()))
        log(group_id, "CLAIM", "first writer", resolved, c)
        return group_id

    if Path(held).resolve() == Path(resolved).resolve():
        return group_id  # same repo, normal re-index

    reason = (f"group '{group_id}' is owned by {held}; refusing to merge "
              f"{resolved} into it")
    log(group_id, "SKIP", reason, resolved, c)
    raise GroupConflict(
        f"{reason}.\n"
        f"  Writing here would invalidate the other repo's facts and merge two\n"
        f"  codebases into one temporal history.\n"
        f"  Use a distinct group:  chronos --group {derive(resolved)} --repo {resolved} index\n"
        f"  Or release the old claim:  chronos release-group {group_id}")


def release(group_id: str, con=None) -> bool:
    """Drop a claim so another repo can take the group. True if one existed."""
    c = _con(con)
    held = owner(group_id, c)
    if held is None:
        return False
    c.execute("DELETE FROM group_ownership WHERE group_id=?", (group_id,))
    log(group_id, "RELEASE", f"was {held}", held, c)
    return True


def resolve(explicit: str | None, repo: str | Path | None) -> str:
    """The group to use, given an explicit --group/env value and a repo.

    Explicit always wins -- an operator naming a group means it. Otherwise the
    group is derived from the repo path, which is what makes the collision
    impossible without anyone having to know the flag exists.

    The literal "default" is treated as unset: it is argparse's old default and
    the exact value that caused every repo to share one namespace. An operator
    who genuinely wants a group called "default" can set CHRONOS_GROUP_ID.
    """
    if explicit and explicit != "default":
        return explicit
    if repo:
        return derive(repo)
    env = os.environ.get("CHRONOS_GROUP_ID")
    if env and env != "default":
        return env
    return "default"
