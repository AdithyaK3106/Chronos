"""Rebuild the NovaPay demo graph by indexing every commit in order.

Each `chronos index` call diffs against the live graph and closes
(invalid_at) edges that vanished, stamped at that commit's real timestamp.
Walking history commit-by-commit is what turns "requests -> httpx" into an
actual temporal fact instead of a single current-state snapshot.

ponytail: one-shot build script, run once to seed the demo repo. Not meant
to be re-run against a live/growing repo -- `chronos index` on HEAD does that.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent / "novapay"
ROOT = Path(__file__).resolve().parent.parent

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
    env = os.environ.copy()
    env["CHRONOS_SQLITE"] = str(REPO / ".chronos" / "chronos.db")
    env["CHRONOS_DB"] = str(REPO / ".chronos" / "graph.kz")

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
    print("done. graph reflects full commit history at", env["CHRONOS_DB"])


if __name__ == "__main__":
    main()
