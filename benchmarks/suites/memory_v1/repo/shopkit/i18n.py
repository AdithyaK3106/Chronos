"""Customer-facing text. Keys live in locale/<lang>.json."""
import json
from pathlib import Path

LANG = "en"


def _(key: str, **kw) -> str:
    catalog = json.loads((Path(__file__).parent.parent / "locale" / f"{LANG}.json").read_text())
    return catalog[key].format(**kw)
