"""Single entry point for the terminal comparison demo: build the graph,
seed the rules, run the comparison + real enforcement, sync the result into
comparison.html, and open it.

ponytail: one script instead of a tmux/asciinema rig -- comparison.html is
already the two-pane replay with live counters that Option 1 asks for, so
this just makes producing/opening it a single command instead of four.
"""

import json
import re
import subprocess
import sys
import webbrowser
from pathlib import Path

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent


def run(*args):
    print(f"$ {' '.join(str(a) for a in args)}")
    r = subprocess.run([sys.executable, *args], cwd=ROOT)
    if r.returncode != 0:
        raise SystemExit(r.returncode)


def sync_html():
    trace = json.loads((DEMO / "trace.json").read_text(encoding="utf-8"))
    html = (DEMO / "comparison.html").read_text(encoding="utf-8")
    new_line = "const DATA = " + json.dumps(trace) + ";"
    new_html = re.sub(r"const DATA = \{.*?\};", lambda m: new_line, html, count=1, flags=re.DOTALL)
    (DEMO / "comparison.html").write_text(new_html, encoding="utf-8")


def main():
    if "--skip-index" not in sys.argv:
        run(str(DEMO / "build_graph.py"))
    run(str(DEMO / "seed_rules.py"))
    run(str(DEMO / "run_comparison.py"))
    sync_html()
    print(f"opening {DEMO / 'comparison.html'}")
    webbrowser.open((DEMO / "comparison.html").resolve().as_uri())


if __name__ == "__main__":
    main()
