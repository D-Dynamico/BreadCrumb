"""Rupee amounts: stored as integer paise, shown with Indian digit grouping."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_AMOUNT = re.compile(r"^\d+(\.\d{1,2})?$")


def format_inr(paise: int) -> str:
    """12345678 -> '1,23,456.78' (lakh and crore grouping)."""
    rupees, rest = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups: list[str] = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join([*groups, tail])
    sign = "-" if paise < 0 else ""
    return f"{sign}{digits}.{rest:02d}"


def parse_amount(text: str) -> int:
    """Accepts '123456.78', '1,23,456.78', '₹ 1,23,456' or 'INR 500'. Returns paise."""
    cleaned = re.sub(r"(?i)^\s*(₹|inr|rs\.?)\s*", "", text.strip()).replace(",", "")
    if not _AMOUNT.match(cleaned):
        raise ValueError("Enter an amount like 12500.00")
    try:
        return int(Decimal(cleaned) * 100)
    except InvalidOperation as exc:
        raise ValueError("Enter an amount like 12500.00") from exc
