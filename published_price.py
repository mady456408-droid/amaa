"""Helpers for published price storage and price-drop reporting."""

from __future__ import annotations

import re
from typing import Any

from coupon_price import parse_price_number

_NUMBER_EMOJIS = (
    "1️⃣",
    "2️⃣",
    "3️⃣",
    "4️⃣",
    "5️⃣",
    "6️⃣",
    "7️⃣",
    "8️⃣",
    "9️⃣",
    "🔟",
)


def detect_currency(price_text: str | None) -> str:
    if not price_text:
        return "EGP"
    upper = price_text.upper()
    if "USD" in upper or "$" in price_text:
        return "USD"
    if "EUR" in upper or "€" in price_text:
        return "EUR"
    if "GBP" in upper or "£" in price_text:
        return "GBP"
    if "EGP" in upper or "جنيه" in price_text:
        return "EGP"
    return "EGP"


def extract_published_price_fields(
    price: str,
    list_price: str | None = None,
) -> dict[str, Any]:
    """Build published price columns from display strings available at publish time."""
    currency = detect_currency(price)
    list_val = parse_price_number(list_price) if list_price else None
    return {
        "published_price": price or None,
        "published_price_value": parse_price_number(price) if price else None,
        "published_list_price": list_price or None,
        "published_list_price_value": list_val,
        "published_currency": currency,
    }


def format_currency_amount(value: float, currency: str = "EGP") -> str:
    """Format numeric amount for price-drop reports (e.g. EGP 14,999)."""
    if abs(value - round(value)) < 0.01:
        amount = f"{int(round(value)):,}"
    else:
        amount = f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{currency} {amount}"


def format_savings(value: float, currency: str = "EGP") -> str:
    """Format savings with sign (e.g. -1,000 EGP)."""
    if abs(value - round(value)) < 0.01:
        amount = f"{int(round(abs(value))):,}"
    else:
        amount = f"{abs(value):,.2f}".rstrip("0").rstrip(".")
    sign = "-" if value > 0 else "+"
    return f"{sign}{amount} {currency}"


def short_title(title: str, max_len: int = 60) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def drop_index_emoji(index: int) -> str:
    if 1 <= index <= len(_NUMBER_EMOJIS):
        return _NUMBER_EMOJIS[index - 1]
    return f"{index}."
