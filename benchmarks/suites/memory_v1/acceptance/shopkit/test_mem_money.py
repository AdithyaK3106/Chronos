import shopkit.i18n as i18n
from shopkit.cart import Item
from shopkit.receipt import render


def teardown_function():
    i18n.LANG = "en"


def test_tax_and_total():
    out = render([Item("mug", 1000)])
    assert "$0.80" in out and "$10.80" in out


def test_tax_rounds_half_up_to_the_cent():
    out = render([Item("x", 1019)])  # 81.52 cents -> 82
    assert "$0.82" in out and "$11.01" in out


def test_tax_line_is_translated():
    en = render([Item("mug", 1000)])
    i18n.LANG = "de"
    de = render([Item("mug", 1000)])
    assert "$0.80" in de and "$10.80" in de
    tax_en = [l for l in en.splitlines() if "$0.80" in l][0]
    tax_de = [l for l in de.splitlines() if "$0.80" in l][0]
    assert tax_en != tax_de, "the tax line is not translated (German receipt shows English text)"
