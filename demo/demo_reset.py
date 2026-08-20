"""make demo-reset: restore novapay's committed .chronos/ state and remove
any scratch files a demo run left behind (new_endpoint.py from the
enforcement scenario), so the demo can be run again cleanly.

ponytail: `git checkout -- .chronos` is the reset -- graph.kz and chronos.db
are tracked fixture files, so git already knows how to restore them. No
separate backup/restore mechanism needed.
"""

import subprocess
import sys
from pathlib import Path

NOVAPAY = Path(__file__).resolve().parent / "novapay"


def main():
    scratch = NOVAPAY / "src" / "payments" / "new_endpoint.py"
    scratch.unlink(missing_ok=True)

    # chronos-audit.log isn't tracked (it's a running append-only log, not a
    # fixture) -- git checkout/clean below won't touch it, so drop it here or
    # a re-run of the demo appends onto a log from the previous run.
    (NOVAPAY / ".chronos" / "chronos-audit.log").unlink(missing_ok=True)

    r = subprocess.run(["git", "checkout", "--", ".chronos", "src"], cwd=NOVAPAY,
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout, r.stderr, file=sys.stderr)
        sys.exit(1)

    r = subprocess.run(["git", "clean", "-fd", ".chronos", "src"], cwd=NOVAPAY,
                       capture_output=True, text=True)
    print(r.stdout.strip() or "nothing to clean")
    print("reset: novapay's .chronos/ and src/ match the committed state")


if __name__ == "__main__":
    main()
