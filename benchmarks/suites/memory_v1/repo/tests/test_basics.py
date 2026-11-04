from shopkit.cart import Item, User
from shopkit.checkout import place_order
from shopkit.receipt import render


def test_order():
    assert place_order([Item("mug", 1200)], User("u1"))["total_cents"] == 1200


def test_receipt():
    assert "Subtotal: $12.00" in render([Item("mug", 1200)])
