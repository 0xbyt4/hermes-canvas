from __future__ import annotations

from canvas.model import Box


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def plan(*specs: dict) -> list[Box]:
    return [Box.from_plan(s) for s in specs]


SHOP_PLAN = (
    {"id": "api", "label": "Payment API", "files": ["src/payments/api/**"], "check": "npm test -- api"},
    {"id": "ui", "label": "Checkout UI", "files": ["src/checkout/*.tsx"], "after": ["api"]},
    {"id": "refund", "label": "Refund page", "files": ["src/refund/**"], "after": ["api"]},
)
