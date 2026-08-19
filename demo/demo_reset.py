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
