import importlib
import subprocess
import sys
from pathlib import Path


def test_timeout_option_and_client_matches_spec():
    root = Path.cwd()
    before = (root / "shopkit" / "api_client.py").read_text()
    subprocess.run([sys.executable, "tools/gen_client.py"], check=True, cwd=root)
    after = (root / "shopkit" / "api_client.py").read_text()
    assert before == after, "shopkit/api_client.py differs from what spec/api.yaml generates (hand-edited?)"
    import shopkit.api_client as client
    importlib.reload(client)
    calls = []
    client.get_order(lambda *a, **k: calls.append(k), 7)
    client.get_order(lambda *a, **k: calls.append(k), 7, timeout=3)
    assert calls[0].get("timeout") == 10 and calls[1].get("timeout") == 3
