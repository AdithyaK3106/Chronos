"""make demo-scenario-1 -- "The Deprecated Pattern": show an agent writing
requests.post(...) under the retired notify_provider_sync name and getting
really blocked by chronos-enforce.

If a recording exists (demo/scenario_1.cast) and `asciinema` is installed,
replays it. Otherwise runs the real scripted session live and prints the
real enforce verdict -- never a canned transcript standing in for a missing
recording. To make/refresh the recording:

    asciinema rec demo/scenario_1.cast -c "python demo/demo_scenario_1.py --live"
"""

import shutil
import subprocess
import sys
from pathlib import Path

DEMO = Path(__file__).resolve().parent
CAST = DEMO / "scenario_1.cast"


def run_live():
    print("$ cat src/payments/new_endpoint.py")
    print("import requests\n")
    print("def notify_provider_sync(order_id, amount_cents):")
    print('    resp = requests.post(PAYMENT_URL, json={"order_id": order_id, "amount": amount_cents})')
    print("    return resp.json()\n")
    print("$ git add src/payments/new_endpoint.py && git commit -m 'add payment notify endpoint'")
    print("$ chronos enforce --repo . --diff HEAD~1\n")
    r = subprocess.run([sys.executable, str(DEMO / "run_comparison.py")], cwd=DEMO.parent)
    sys.exit(r.returncode)


def main():
    if "--live" not in sys.argv and CAST.exists() and shutil.which("asciinema"):
        subprocess.run(["asciinema", "play", str(CAST)])
        return
    if "--live" not in sys.argv and not CAST.exists():
        print(f"no recording at {CAST} yet -- running the real scripted session live instead.\n"
              f"to record one: asciinema rec {CAST} -c \"python {Path(__file__).name} --live\"\n")
    run_live()


if __name__ == "__main__":
    main()
