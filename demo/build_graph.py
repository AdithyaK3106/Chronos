"""Rebuild the NovaPay demo graph by indexing every commit in order.

Each `chronos index` call diffs against the live graph and closes
(invalid_at) edges that vanished, stamped at that commit's real timestamp
(chronos/cli.py::commit_time -- HEAD's git commit time, UNLESS the working
tree is dirty, in which case it falls back to now()). Walking history
commit-by-commit is what turns "requests -> httpx" into an actual temporal
fact instead of a single current-state snapshot.

The graph is built OUTSIDE novapay's own working tree (STAGING, below) and
copied into novapay/.chronos/ only at the end. Building in-place would make
`git status --porcelain` see the graph file being written as uncommitted
changes on every commit in the walk -- a permanently dirty tree -- which
silently kills every fact's real timestamp in favor of now(). Found the hard
way: every as-of query returned "predates_earliest_record" for dates that
should have had history.

ponytail: one-shot build script, run once to seed the demo repo. Not meant
to be re-run against a live/growing repo -- `chronos index` on HEAD does that.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / "novapay"
ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "demo" / ".chronos-build"  # outside REPO's working tree, see module docstring

# Fixed, not derived from REPO's absolute path -- a path-derived group id
# would differ on every clone, breaking every script and .mcp.json that
# names it explicitly. See chronos/groups.py::derive.
GROUP = "novapay-demo"


def sh(*args, cwd=REPO, check=True):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        raise SystemExit(f"failed: {args}")
    return r.stdout.strip()


def main():
    if STAGING.exists():
        shutil.rmtree(STAGING)
    STAGING.mkdir(parents=True)

    env = os.environ.copy()
    env["CHRONOS_SQLITE"] = str(STAGING / "chronos.db")
    env["CHRONOS_DB"] = str(STAGING / "graph.kz")

    commits = sh("git", "log", "--reverse", "--format=%H").splitlines()
    branch = sh("git", "branch", "--show-current")

    for i, sha in enumerate(commits, 1):
        sh("git", "checkout", "-q", sha)
        print(f"[{i}/{len(commits)}] indexing {sha[:8]} ...")
        r = subprocess.run(
            [sys.executable, "-m", "chronos", "--group", GROUP, "index", "--repo", str(REPO)],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        print(r.stdout.strip())
        if r.returncode != 0:
            print(r.stderr, file=sys.stderr)
            raise SystemExit(1)

    sh("git", "checkout", "-q", branch)

    dest = REPO / ".chronos"
    dest.mkdir(exist_ok=True)
    shutil.copy2(STAGING / "graph.kz", dest / "graph.kz")
    shutil.copy2(STAGING / "chronos.db", dest / "chronos.db")
    shutil.rmtree(STAGING)
    print("done. graph reflects full commit history at", dest / "graph.kz")


if __name__ == "__main__":
    main()
