"""Regressions for the bugs the end-to-end pilot found (2026-08-14).

Each test pins one fix. They are cheap and offline: no LLM, no network, no
indexer. The bugs they cover were all *silent* -- wrong output or a no-op with
no error -- which is exactly the class a test has to hold down, because nothing
else will notice.

Run: pytest tests/test_pilot_regressions.py -q
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from chronos import db, indexer, sync, upstream  # noqa: E402
from chronos.cli import commit_time  # noqa: E402


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, **kw)


def _repo(tmp_path, when="2026-08-05T19:48:20+05:30"):
    """A one-commit git repo whose HEAD has a non-UTC committer time."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("hi\n", encoding="utf-8")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-qm", "init",
         env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
    return r


# --- BUG-6: commit timezone was dropped instead of converted ---------------

def test_commit_time_converts_offset_to_utc(tmp_path):
    """+05:30 must become UTC, not have its offset truncated.

    The bug kept the wall-clock digits (19:48:20+05:30 -> 19:48:20+00:00),
    recording every fact 5h30m into the future and silently corrupting every
    as-of query in that window."""
    at = commit_time(str(_repo(tmp_path)))
    assert at.utcoffset() == timedelta(0), "commit_time must return UTC"
    assert at.isoformat().startswith("2026-08-05T14:18:20"), (
        f"19:48:20+05:30 is 14:18:20Z; got {at.isoformat()}")


# --- BUG-7: dirty tree reused HEAD's time, so valid_at == invalid_at -------

def test_dirty_tree_is_stamped_now_not_head(tmp_path):
    """An uncommitted edit is not part of HEAD and must not claim HEAD's time.

    Reusing it meant a fact created and superseded between two commits got
    valid_at == invalid_at -- no T at which it was ever true."""
    r = _repo(tmp_path)
    clean = commit_time(str(r))
    (r / "a.txt").write_text("changed\n", encoding="utf-8")
    dirty = commit_time(str(r))
    assert dirty > clean, "a dirty tree must be stamped later than HEAD"
    assert (datetime.now(timezone.utc) - dirty).total_seconds() < 120


def test_clean_tree_still_uses_head_time(tmp_path):
    """The dirty-tree fix must not break the reason commit_time exists:
    re-syncing an old checkout still reports the checkout's age."""
    at = commit_time(str(_repo(tmp_path)))
    assert at.year == 2026 and at.month == 8 and at.day == 5


# --- BUG-5: sync dropped qname on the way to the graph ---------------------

def test_sync_preserves_qname_in_attributes():
    """qname is what node_identity keys on; dropping it made the graph's own
    identity unqueryable after a sync."""
    n = {"name": "f", "path": "a.py", "kind": "Function",
         "qname": "proj.a.f", "language": "python"}
    assert sync.node_identity(n) == "proj.a.f::Function"
    src = Path(sync.__file__).read_text(encoding="utf-8")
    assert '"qname": n.get("qname"' in src, (
        "attributes is an explicit whitelist -- qname must be listed or it is "
        "silently dropped at the graph boundary")


# --- BUG-1: doctor reported whichever cached DB was newest ------------------

def test_find_db_never_returns_another_repos_index(tmp_path):
    """Naming a repo must yield that repo's index or None -- never a different
    repo's, which is what the newest-by-mtime scan did."""
    cache = tmp_path / "cache"
    cache.mkdir()
    other = cache / f"{upstream.repo_slug(tmp_path / 'other')}.db"
    other.write_bytes(b"")
    mine = tmp_path / "mine"
    assert upstream.find_db(cache_dir=cache, repo=mine) is None, (
        "an unindexed repo must report nothing, not someone else's database")
    own = cache / f"{upstream.repo_slug(mine)}.db"
    own.write_bytes(b"")
    assert upstream.find_db(cache_dir=cache, repo=mine) == own
    # No repo named -> the old behaviour is still available.
    assert upstream.find_db(cache_dir=cache) is not None


def test_repo_slug_matches_upstream_naming():
    """Separators and spaces collapse to '-', drive colon is dropped."""
    s = upstream.repo_slug("C:/Users/u/Projects/Cooling project")
    assert ":" not in s and " " not in s
    assert s.endswith("Cooling-project")


# --- BUG-9: CLI and MCP resolved different SQLite stores -------------------

def test_db_path_honours_repo_config(tmp_path, monkeypatch):
    """Every entry point must land on the same database.

    The CLI read <repo>/.chronos/config.json while the MCP tools used the
    global default, so rules created via MCP were invisible to CI -- which
    reported a clean pass on a blocking violation."""
    repo = tmp_path / "r"
    (repo / ".chronos").mkdir(parents=True)
    want = repo / ".chronos" / "chronos.db"
    (repo / ".chronos" / "config.json").write_text(
        json.dumps({"repo": str(repo), "chronos_sqlite": str(want)}), encoding="utf-8")
    monkeypatch.delenv("CHRONOS_SQLITE", raising=False)
    monkeypatch.delenv("CHRONOS_LEDGER", raising=False)
    monkeypatch.setenv("CHRONOS_REPO_PATH", str(repo))
    assert db.db_path() == want


def test_explicit_env_still_wins_over_repo_config(tmp_path, monkeypatch):
    """An operator who exported CHRONOS_SQLITE meant it."""
    repo = tmp_path / "r"
    (repo / ".chronos").mkdir(parents=True)
    (repo / ".chronos" / "config.json").write_text(
        json.dumps({"chronos_sqlite": str(repo / "from_config.db")}), encoding="utf-8")
    explicit = tmp_path / "explicit.db"
    monkeypatch.delenv("CHRONOS_LEDGER", raising=False)
    monkeypatch.setenv("CHRONOS_REPO_PATH", str(repo))
    monkeypatch.setenv("CHRONOS_SQLITE", str(explicit))
    assert db.db_path() == explicit


def test_malformed_repo_config_falls_back_to_default(tmp_path, monkeypatch):
    """A broken config.json must not take the database down with it."""
    repo = tmp_path / "r"
    (repo / ".chronos").mkdir(parents=True)
    (repo / ".chronos" / "config.json").write_text("{not json", encoding="utf-8")
    monkeypatch.delenv("CHRONOS_SQLITE", raising=False)
    monkeypatch.delenv("CHRONOS_LEDGER", raising=False)
    monkeypatch.setenv("CHRONOS_REPO_PATH", str(repo))
    assert db.db_path().name == "chronos.db"   # global default, no exception


# --- BUG-3: the post-merge hook init writes exited 2 every time ------------

@pytest.mark.parametrize("cmd", ["index", "sync", "watch", "health", "doctor"])
def test_repo_flag_accepted_after_subcommand(cmd):
    """init's hooks put --repo after the subcommand. `index` had no such flag,
    so the post-merge hook failed on every merge and re-indexing silently
    stopped."""
    out = subprocess.run([sys.executable, "-m", "chronos", cmd, "--repo", ".", "--help"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"chronos {cmd} --repo rejected: {out.stderr[-300:]}"


def test_init_hooks_use_a_working_flag_order():
    """Pin the hook bodies against the CLI they invoke."""
    from chronos.cli import HOOKS
    for name, body in HOOKS.items():
        assert "--repo" in body
        sub = "index" if name == "post-merge" else "enforce"
        assert f"chronos {sub} --repo" in body, f"{name} hook shape changed"


# --- BUG-11: rule_report raised TypeError on a non-int ---------------------

def test_rule_report_rejects_non_numeric_window():
    """An LLM passing a rule id (the name invites it) must get a structured
    error, not a raw TypeError."""
    import asyncio

    from chronos import wedge4_mcp
    r = asyncio.run(wedge4_mcp.chronos_rule_report("some-rule-id"))
    assert "error" in r and "rule id" in r["error"]
    assert "error" in asyncio.run(wedge4_mcp.chronos_rule_report(-1))
    assert "blocks" in asyncio.run(wedge4_mcp.chronos_rule_report("7"))  # coerced


# --- Language coverage: unmapped extensions were skipped silently ----------

def test_web_extensions_are_mapped():
    """.html/.css/.json resolved to 'unknown', so every rule skipped them."""
    for ext, lang in [(".html", "html"), (".css", "css"), (".json", "json")]:
        assert indexer.node_language(f"a{ext}") == lang


def test_unsupported_language_stays_unknown():
    """ast-grep 0.45.1 cannot parse language: scss. Mapping it would swap a
    silent skip for a broken scan, which is worse."""
    assert indexer.node_language("a.scss") == "unknown"
    assert indexer.node_language("a.bin") == "unknown"


# --- BUG-10: the generator emitted invalid ast-grep metavariable syntax ----

def test_generator_prompt_teaches_metavariable_syntax():
    """ast-grep accepts `print($ARGS..)` silently (exit 0, zero matches), so
    CHECK A cannot catch it. The prompt has to prevent it up front."""
    from chronos.rule_generator import PROMPT
    assert "$$$" in PROMPT
    assert "$ARGS.." in PROMPT and "INVALID" in PROMPT
