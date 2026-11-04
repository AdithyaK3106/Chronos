from pathlib import Path

from shopkit import flags
from shopkit.cart import User
from shopkit.checkout import one_click_available


def teardown_function():
    flags.clear_overrides()


def _new_flag():
    keys = {l.split(":")[0].strip() for l in Path("flags.yaml").read_text().splitlines() if ":" in l and not l.strip().startswith("#")}
    new = keys - {"gift_wrap", "express_shipping"}
    assert len(new) == 1, f"expected one new flag in flags.yaml, found {sorted(new)}"
    return new.pop()


def test_beta_users_only():
    assert one_click_available(User("a", ["beta"]))
    assert not one_click_available(User("b", []))


def test_controlled_by_the_flag():
    name = _new_flag()
    flags.override(name, ["all"])
    assert one_click_available(User("b", []))
    flags.override(name, [])
    assert not one_click_available(User("a", ["beta"]))
