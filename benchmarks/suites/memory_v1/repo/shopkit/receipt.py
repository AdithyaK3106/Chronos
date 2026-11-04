from shopkit.cart import subtotal
from shopkit.i18n import _
from shopkit.money import format_money


def render(items, gift_wrap: bool = False) -> str:
    lines = [_("receipt.title")]
    for i in items:
        lines.append(f"{i.qty} x {i.name}  {format_money(i.price_cents * i.qty)}")
    lines.append(_("receipt.subtotal", amount=format_money(subtotal(items))))
    if gift_wrap:
        lines.append(_("receipt.gift_wrap"))
    return "\n".join(lines)
