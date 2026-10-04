"""What a value is (money, a date, an email address) and when two values are the same.

Used by the gateway's provenance check, by reconcile and by the verifier, so "is
this the same value" has one answer everywhere. Pure functions, no I/O.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_NUMBER = re.compile(r"^(?:₹|INR|Rs\.?)?\s*-?[\d,]*\d(\.\d+)?\s*$", re.IGNORECASE)
# Money as people write it: two decimals, or a currency marker. A bare "10" is a
# quantity as often as an amount, so it is not treated as money.
_MONEY = re.compile(
    r"^(?:(?:₹|INR|Rs\.?)\s*-?[\d,]*\d(\.\d+)?|-?[\d,]*\d\.\d{2})\s*$", re.IGNORECASE
)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_DATE_FORMATS = (
    "%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y",
    "%B %d, %Y", "%d %b, %Y", "%d-%b-%Y", "%Y/%m/%d",
)  # fmt: skip


def number(value: object) -> Decimal | None:
    text = str(value).strip()
    if not _NUMBER.match(text):
        return None
    cleaned = re.sub(r"^(₹|INR|Rs\.?)", "", text, flags=re.IGNORECASE).replace(",", "")
    try:
        return Decimal(cleaned.strip())
    except InvalidOperation:
        return None


def as_date(value: object) -> date | None:
    text = " ".join(str(value).split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def is_money(value: object) -> bool:
    return bool(_MONEY.match(str(value).strip()))


def is_email(value: object) -> bool:
    return bool(_EMAIL.match(str(value).strip()))


def kind_of(value: object) -> str:
    """money, date, email or text: how a typed value is checked for provenance."""
    if as_date(value) is not None:
        return "date"
    if is_money(value):
        return "money"
    if is_email(value):
        return "email"
    return "text"


def same_value(a: object, b: object) -> bool:
    """Equal as the apps would mean it: numbers by value, dates by day, text ignoring
    case and spacing."""
    na, nb = number(a), number(b)
    if na is not None and nb is not None:
        return na == nb
    da, db = as_date(a), as_date(b)
    if da is not None and db is not None:
        return da == db
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()
