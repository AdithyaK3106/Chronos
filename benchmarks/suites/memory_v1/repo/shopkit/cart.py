from dataclasses import dataclass, field


@dataclass
class Item:
    name: str
    price_cents: int
    qty: int = 1


@dataclass
class User:
    id: str
    groups: list = field(default_factory=list)


def subtotal(items) -> int:
    return sum(i.price_cents * i.qty for i in items)
