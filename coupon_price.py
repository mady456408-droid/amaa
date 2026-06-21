"""Apply coupon discounts to displayed caption prices (not raw scrape values)."""

import logging
import re
from typing import Literal, TypedDict

logger = logging.getLogger(__name__)

_INVISIBLE = ("\u200f", "\u200e", "\u202a", "\u202b", "\u202c", "\ufeff", "\xa0")

_PRICE_EPSILON = 0.01


class CouponPriceResult(TypedDict):
    success: bool
    final_price: str
    coupon_suffix: str | None
    original_price: str


def _strip_invisible(text: str) -> str:
    out = text
    for ch in _INVISIBLE:
        out = out.replace(ch, "")
    return re.sub(r"\s+", " ", out).strip()


def parse_price_number(price_text: str) -> float | None:
    """Extract numeric price from scraped Amazon price strings."""
    if not price_text:
        return None
    t = _strip_invisible(price_text)
    t = re.sub(r"(?i)(جنيه|egp|£|usd|eur)", "", t).strip()
    m = re.search(r"([\d,]+(?:\.\d+)?)", t)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_coupon_discount(coupon_text: str) -> tuple[str, float] | None:
    """Return ('percent', value) or ('fixed', amount) from normalized coupon text."""
    t = _strip_invisible(coupon_text)
    if not t:
        return None

    m = re.search(r"خصم\s*([\d,]+(?:\.\d+)?)\s*%", t)
    if m:
        return ("percent", float(m.group(1).replace(",", "")))

    m = re.search(r"إضافي\s*([\d,]+(?:\.\d+)?)\s*جنيه", t)
    if m:
        return ("fixed", float(m.group(1).replace(",", "")))

    m = re.search(r"خصم\s*([\d,]+(?:\.\d+)?)\s*جنيه", t)
    if m:
        return ("fixed", float(m.group(1).replace(",", "")))

    m = re.search(r"([\d,]+(?:\.\d+)?)\s*%", t)
    if m and "خصم" in t:
        return ("percent", float(m.group(1).replace(",", "")))

    return None


def _format_display_amount(amount: float) -> str:
    if abs(amount - round(amount)) < 0.01:
        return str(int(round(amount)))
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return text


def _clean_price_for_display(price_text: str) -> str:
    n = parse_price_number(price_text)
    if n is not None:
        return _format_display_amount(n)
    t = re.sub(r"(?i)(جنيه|egp|£)", "", _strip_invisible(price_text)).strip()
    return t or price_text.strip()


def _failed_result(original: str) -> CouponPriceResult:
    return CouponPriceResult(
        success=False,
        final_price=original,
        coupon_suffix=None,
        original_price=original,
    )


def _compute_discounted_price(
    base: float, discount: tuple[str, float]
) -> float:
    kind, value = discount
    if kind == "percent":
        return max(0.0, base * (1.0 - value / 100.0))
    return max(0.0, base - value)


def _display_price_reflects_coupon(
    display_parsed: float,
    list_parsed: float,
    discount: tuple[str, float],
) -> bool:
    """True when list - coupon ≈ display (coupon already baked into scraped price)."""
    if list_parsed <= display_parsed + _PRICE_EPSILON:
        return False
    expected = _compute_discounted_price(list_parsed, discount)
    return abs(expected - display_parsed) < _PRICE_EPSILON


def coupon_apply_kwargs_from_product(product: dict | None) -> dict:
    """Keyword args for apply_coupon_to_price from a scrape product dict."""
    if not product:
        return {}
    kwargs: dict = {}
    if product.get("list_price"):
        kwargs["list_price_text"] = product["list_price"]
    if product.get("coupon_already_applied"):
        kwargs["coupon_already_applied"] = True
    return kwargs


def _log_coupon_price_debug(
    *,
    original: str,
    coupon_text: str | None,
    parsed_price: float | None,
    parsed_coupon: tuple[str, float] | None,
    result: CouponPriceResult,
) -> None:
    parsed_coupon_repr = None
    if parsed_coupon:
        parsed_coupon_repr = f"{parsed_coupon[0]}={parsed_coupon[1]}"
    logger.info(
        "COUPON PRICE DEBUG original=%r coupon=%r parsed_price=%r "
        "parsed_coupon=%r final=%r success=%r",
        original,
        coupon_text,
        parsed_price,
        parsed_coupon_repr,
        result["final_price"],
        result["success"],
    )


def _log_caption_debug(
    debug_path: str,
    price_text: str,
    coupon_text: str | None,
    result: CouponPriceResult,
    line: str,
    *,
    suffix_appended: bool,
) -> None:
    logger.info(
        "CAPTION DEBUG path=%s raw_price=%r coupon=%r success=%s "
        "suffix_appended=%s reason=%s line=%r",
        debug_path,
        price_text,
        coupon_text,
        result["success"],
        suffix_appended,
        "coupon applied to caption price"
        if suffix_appended
        else "no suffix (success=False or no coupon)",
        line,
    )


def apply_coupon_to_price(
    price_text: str,
    coupon_text: str | None,
    *,
    list_price_text: str | None = None,
    coupon_already_applied: bool = False,
) -> CouponPriceResult:
    """
    Compute displayed price after coupon.

    success is True only when price and coupon parse, discount is applied,
    the final price differs from the original scraped price, and the scraped
    display price does not already include the coupon discount.
    """
    original = _clean_price_for_display(price_text)
    price_parsed = parse_price_number(price_text)
    discount = _parse_coupon_discount(coupon_text) if coupon_text else None

    if not coupon_text:
        result = _failed_result(original)
        _log_coupon_price_debug(
            original=original,
            coupon_text=coupon_text,
            parsed_price=price_parsed,
            parsed_coupon=None,
            result=result,
        )
        return result

    if coupon_already_applied:
        logger.info(
            "COUPON PRICE SKIPPED: page indicates coupon already applied"
        )
        result = _failed_result(original)
        _log_coupon_price_debug(
            original=original,
            coupon_text=coupon_text,
            parsed_price=price_parsed,
            parsed_coupon=discount,
            result=result,
        )
        return result

    if price_parsed is None or discount is None:
        logger.info(
            "COUPON PRICE SKIPPED: parse failed (price=%r coupon=%r)",
            price_text,
            coupon_text,
        )
        result = _failed_result(original)
        _log_coupon_price_debug(
            original=original,
            coupon_text=coupon_text,
            parsed_price=price_parsed,
            parsed_coupon=discount,
            result=result,
        )
        return result

    list_parsed = parse_price_number(list_price_text) if list_price_text else None
    if list_parsed is not None and _display_price_reflects_coupon(
        price_parsed, list_parsed, discount
    ):
        logger.info(
            "COUPON PRICE SKIPPED: display price already reflects coupon "
            "(list=%r display=%r coupon=%r)",
            list_price_text,
            price_text,
            coupon_text,
        )
        result = _failed_result(original)
        _log_coupon_price_debug(
            original=original,
            coupon_text=coupon_text,
            parsed_price=price_parsed,
            parsed_coupon=discount,
            result=result,
        )
        return result

    kind, value = discount
    if kind == "percent":
        if value <= 0 or value >= 100:
            logger.info("COUPON PRICE SKIPPED: invalid percent %s", value)
            result = _failed_result(original)
            _log_coupon_price_debug(
                original=original,
                coupon_text=coupon_text,
                parsed_price=price_parsed,
                parsed_coupon=discount,
                result=result,
            )
            return result
        new_price = _compute_discounted_price(price_parsed, discount)
    else:
        if value <= 0:
            logger.info("COUPON PRICE SKIPPED: invalid fixed amount %s", value)
            result = _failed_result(original)
            _log_coupon_price_debug(
                original=original,
                coupon_text=coupon_text,
                parsed_price=price_parsed,
                parsed_coupon=discount,
                result=result,
            )
            return result
        new_price = _compute_discounted_price(price_parsed, discount)

    if abs(new_price - price_parsed) < _PRICE_EPSILON:
        logger.info(
            "COUPON PRICE SKIPPED: no price change (raw=%s coupon=%r)",
            original,
            coupon_text,
        )
        result = _failed_result(original)
        _log_coupon_price_debug(
            original=original,
            coupon_text=coupon_text,
            parsed_price=price_parsed,
            parsed_coupon=discount,
            result=result,
        )
        return result

    suffix = (
        coupon_text if coupon_text.startswith("ب") else f"ب{coupon_text}"
    )
    final = _format_display_amount(new_price)
    result = CouponPriceResult(
        success=True,
        final_price=final,
        coupon_suffix=suffix,
        original_price=original,
    )
    _log_coupon_price_debug(
        original=original,
        coupon_text=coupon_text,
        parsed_price=price_parsed,
        parsed_coupon=discount,
        result=result,
    )
    return result


def effective_coupon_for_caption(
    coupon_text: str | None, result: CouponPriceResult
) -> str | None:
    """Coupon text passed to formatters — only when result.success is True."""
    if result["success"] and coupon_text:
        return coupon_text
    return None


def _format_arabic_from_result(
    result: CouponPriceResult, *, debug_path: str, price_text: str, coupon_text: str | None
) -> str:
    suffix_appended = bool(result["success"] and result["coupon_suffix"])
    if suffix_appended:
        line = f"💰 بسعر {result['final_price']} جنيه {result['coupon_suffix']}"
    else:
        line = f"💰 بسعر {result['final_price']} جنيه"
    _log_caption_debug(
        debug_path,
        price_text,
        coupon_text,
        result,
        line,
        suffix_appended=suffix_appended,
    )
    return line


def _format_standard_from_result(
    result: CouponPriceResult,
    price_text: str,
    *,
    debug_path: str,
    coupon_text: str | None,
) -> str:
    suffix_appended = bool(result["success"] and result["coupon_suffix"])
    if suffix_appended:
        line = f"💰 {result['final_price']} جنيه {result['coupon_suffix']}"
    else:
        clean = _strip_invisible(price_text)
        if re.search(r"(?i)جنيه|egp", clean):
            line = f"💰 {clean}"
        else:
            line = f"💰 {result['final_price']} جنيه"
    _log_caption_debug(
        debug_path,
        price_text,
        coupon_text,
        result,
        line,
        suffix_appended=suffix_appended,
    )
    return line


def format_arabic_price_line(
    price_text: str,
    coupon_text: str | None = None,
    *,
    debug_path: str = "format_arabic_price_line",
    list_price_text: str | None = None,
    coupon_already_applied: bool = False,
) -> str:
    """Single Arabic price line for captions."""
    result = apply_coupon_to_price(
        price_text,
        coupon_text,
        list_price_text=list_price_text,
        coupon_already_applied=coupon_already_applied,
    )
    return _format_arabic_from_result(
        result, debug_path=debug_path, price_text=price_text, coupon_text=coupon_text
    )


def format_standard_price_line(
    price_text: str,
    coupon_text: str | None = None,
    *,
    debug_path: str = "format_standard_price_line",
    list_price_text: str | None = None,
    coupon_already_applied: bool = False,
) -> str:
    """Standard caption price line (off mode / fallback)."""
    result = apply_coupon_to_price(
        price_text,
        coupon_text,
        list_price_text=list_price_text,
        coupon_already_applied=coupon_already_applied,
    )
    return _format_standard_from_result(
        result,
        price_text,
        debug_path=debug_path,
        coupon_text=coupon_text,
    )


def _detect_price_variant(caption: str) -> Literal["arabic", "standard"]:
    if "بسعر" in caption:
        return "arabic"
    return "standard"


def normalize_caption_price_line(
    caption: str,
    price_text: str,
    coupon_text: str | None,
    *,
    debug_path: str = "normalize_caption",
    variant: Literal["arabic", "standard", "auto"] = "auto",
    list_price_text: str | None = None,
    coupon_already_applied: bool = False,
) -> str:
    """
    Replace the first 💰 price line with the canonical line from apply_coupon_to_price.
    Drops standalone 🎟 coupon lines when coupon is merged into the price line.
    """
    result = apply_coupon_to_price(
        price_text,
        coupon_text,
        list_price_text=list_price_text,
        coupon_already_applied=coupon_already_applied,
    )
    effective = effective_coupon_for_caption(coupon_text, result)

    resolved = variant if variant != "auto" else _detect_price_variant(caption)
    if resolved == "arabic":
        canonical = _format_arabic_from_result(
            result,
            debug_path=f"{debug_path}:arabic",
            price_text=price_text,
            coupon_text=effective,
        )
    else:
        canonical = _format_standard_from_result(
            result,
            price_text,
            debug_path=f"{debug_path}:standard",
            coupon_text=effective,
        )

    lines = caption.split("\n")
    new_lines: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("🎟"):
            continue
        if stripped.startswith("💰"):
            if not replaced:
                new_lines.append(canonical)
                replaced = True
            continue
        new_lines.append(line)

    if not replaced:
        new_lines.extend(["", canonical])

    return "\n".join(new_lines).strip()
