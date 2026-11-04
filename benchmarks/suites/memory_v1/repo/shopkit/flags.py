"""Feature flags, declared in flags.yaml as `flag_name: [groups that see it]`."""
from pathlib import Path

_overrides: dict = {}


def _load() -> dict:
    flags = {}
    for line in (Path(__file__).parent.parent / "flags.yaml").read_text().splitlines():
        if ":" in line and not line.strip().startswith("#"):
            name, groups = line.split(":", 1)
            flags[name.strip()] = [g.strip() for g in groups.strip().strip("[]").split(",") if g.strip()]
    return flags


def is_enabled(name: str, user) -> bool:
    groups = _overrides.get(name, _load().get(name))
    if groups is None:
        return False
    return "all" in groups or bool(set(groups) & set(getattr(user, "groups", [])))


def override(name: str, groups):
    _overrides[name] = groups


def clear_overrides():
    _overrides.clear()
