from shopkit import flags
from shopkit.cart import subtotal
from shopkit.errors import ShopError

STOCK = {"mug": 5, "shirt": 2}


def place_order(items, user):
    for i in items:
        if STOCK.get(i.name, 0) < i.qty:
            raise ShopError("OUT_OF_STOCK")
    order = {"user": user.id, "total_cents": subtotal(items), "gift_wrap": flags.is_enabled("gift_wrap", user)}
    order["express"] = flags.is_enabled("express_shipping", user)
    return order
