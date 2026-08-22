"""Helpers for published price storage and price-drop reporting."""

from __future__ import annotations

import logging
import re
from typing import Any

from coupon_price import parse_price_number

logger = logging.getLogger(__name__)

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


def format_detailed_price_drop_message(
    *,
    title: str,
    current_price: float,
    previous_price: float,
    currency: str = "EGP",
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
    coupon: str | None = None,
    seller: str | None = None,
) -> str:
    savings = previous_price - current_price
    drop_pct = (savings / previous_price * 100.0) if previous_price > 0 else 0.0

    curr_fmt = format_currency_amount(current_price, currency)
    prev_fmt = format_currency_amount(previous_price, currency)
    savings_fmt = format_currency_amount(savings, currency)

    if drop_pct >= 10.0:
        header = "🔥 <b>تخفيض قوي!</b>\n"
    elif drop_pct >= 5.0:
        header = "📉 <b>تخفيض ملحوظ!</b>\n"
    else:
        header = "📉 <b>تحديث السعر</b>\n"

    lines = [
        header,
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>السعر الحالي:</b> {curr_fmt}",
        f"📉 <b>كان:</b> {prev_fmt}",
        f"💵 <b>وفرت:</b> {savings_fmt}",
        f"📊 <b>انخفاض:</b> {drop_pct:.1f}%\n",
    ]

    if coupon:
        lines.append(f"🎟 <b>كوبون:</b> {coupon}")
    if seller:
        lines.append(f"🏪 <b>البائع:</b> {seller}")

    if stats and stats.get("has_data"):
        lowest = stats["lowest_price"]
        highest = stats["highest_price"]
        avg = stats["average_price"]

        lowest_fmt = format_currency_amount(lowest, currency)
        highest_fmt = format_currency_amount(highest, currency)
        avg_fmt = format_currency_amount(avg, currency)

        lines.extend([
            "📈 <b>سجل السعر:</b>",
            f"• أعلى سعر مسجل: {highest_fmt}",
            f"• متوسط السعر: {avg_fmt}",
            f"• أقل سعر مسجل: {lowest_fmt}\n",
        ])

        if stats.get("is_lowest"):
            lines.append("🏷️ <b>تنويه:</b> هذا السعر هو أقل سعر مسجل بالمرصد حتى الآن.")

    if product_url:
        lines.extend(["\n🔗 <b>اشترِ من هنا:</b>", product_url])

    return "\n".join(lines)


def generate_price_chart_image(
    asin: str,
    title: str,
    records: list[dict[str, Any]],
) -> str | None:
    if not records or len(records) < 2:
        return None

    try:
        import os
        import tempfile
        from datetime import datetime
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        sorted_recs = sorted(records, key=lambda r: r.get("recorded_at") or "")
        dates = []
        prices = []
        for r in sorted_recs:
            raw_dt = r.get("recorded_at") or ""
            p_raw = r.get("final_price")
            if p_raw is None:
                p_raw = r.get("price_value")
            try:
                p_val = float(p_raw) if p_raw is not None else 0.0
            except (ValueError, TypeError):
                p_val = 0.0

            if p_val <= 0:
                continue

            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                dates.append(dt.strftime("%d %b %H:%M"))
            except Exception:
                dates.append(raw_dt[:10] if raw_dt else "N/A")
            prices.append(p_val)

        if not prices or len(prices) < 2:
            return None

        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=150)
        fig.patch.set_facecolor("#1e1e2e")
        ax.set_facecolor("#1e1e2e")

        ax.plot(dates, prices, color="#89b4fa", linewidth=2.5, marker="o", markersize=5, label="Price")

        min_idx = prices.index(min(prices))
        max_idx = prices.index(max(prices))
        curr_idx = len(prices) - 1

        ax.scatter([dates[min_idx]], [prices[min_idx]], color="#a6e3a1", s=100, zorder=5, label=f"Lowest ({prices[min_idx]:,.0f})")
        ax.scatter([dates[max_idx]], [prices[max_idx]], color="#f38ba8", s=100, zorder=5, label=f"Highest ({prices[max_idx]:,.0f})")
        ax.scatter([dates[curr_idx]], [prices[curr_idx]], color="#f9e2af", s=100, zorder=5, label=f"Current ({prices[curr_idx]:,.0f})")

        ax.set_title(f"Price History — {short_title(title, 40)}", color="#cdd6f4", fontsize=12, pad=12, fontweight="bold")
        ax.set_ylabel("Price (EGP)", color="#cdd6f4", fontsize=10)
        ax.tick_params(colors="#bac2de", labelsize=8)
        plt.xticks(rotation=30, ha="right")
        ax.grid(True, color="#45475a", linestyle="--", alpha=0.5)
        ax.legend(facecolor="#313244", edgecolor="#45475a", labelcolor="#cdd6f4", fontsize=8)

        for spine in ax.spines.values():
            spine.set_color("#45475a")

        plt.tight_layout()

        out_dir = tempfile.gettempdir()
        out_path = os.path.join(out_dir, f"chart_{asin}_{int(datetime.now().timestamp())}.png")
        fig.savefig(out_path, facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        return out_path
    except Exception as exc:
        logger.error("CHART RENDER ERROR asin=%s error_type=%s error=%s", asin, type(exc).__name__, exc, exc_info=True)
        return None


def format_smart_restock_message(
    *,
    title: str,
    current_price: float,
    reference_price: float,
    previous_price: float | None = None,
    currency: str = "EGP",
    product_url: str | None = None,
) -> str:
    ref_discount = reference_price - current_price
    ref_pct = (ref_discount / reference_price * 100.0) if reference_price > 0 else 0.0

    curr_fmt = format_currency_amount(current_price, currency)
    ref_fmt = format_currency_amount(reference_price, currency)

    lines = [
        "♻️ <b>رجع متاح بسعر ممتاز!</b>\n",
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>السعر الحالي:</b> {curr_fmt}",
        f"📊 <b>السعر المرجعي:</b> {ref_fmt}",
        f"🔥 <b>أقل من السعر المرجعي بـ</b> {ref_pct:.1f}%",
    ]

    if previous_price is not None and previous_price > 0:
        prev_fmt = format_currency_amount(previous_price, currency)
        lines.append(f"📉 <b>آخر سعر قبل النفاد:</b> {prev_fmt}\n")
        lines.append("💡 السعر الحالي أعلى من آخر سعر، لكنه ما زال أقل بكثير من السعر المرجعي.")
    else:
        lines.append("")

    if product_url:
        lines.extend(["\n🔗 <b>شوف العرض:</b>", product_url])

    return "\n".join(lines)


def format_resale_smart_restock_message(
    *,
    title: str,
    current_price: float,
    reference_price: float,
    previous_price: float | None = None,
    currency: str = "EGP",
    seller_condition: str | None = None,
    product_url: str | None = None,
) -> str:
    from telegram_publisher import format_resale_condition_arabic
    ref_discount = reference_price - current_price
    ref_pct = (ref_discount / reference_price * 100.0) if reference_price > 0 else 0.0

    curr_fmt = format_currency_amount(current_price, currency)
    ref_fmt = format_currency_amount(reference_price, currency)
    cond_phrase = format_resale_condition_arabic(seller_condition)

    lines = [
        "♻️ <b>Amazon Resale — رجع متاح بسعر ممتاز!</b>\n",
        f"<b>{cond_phrase}</b>",
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>سعر Resale الحالي:</b> {curr_fmt}",
        f"📊 <b>السعر المرجعي لـ Resale:</b> {ref_fmt}",
        f"🔥 <b>أقل من المرجع بـ</b> {ref_pct:.1f}%",
    ]

    if previous_price is not None and previous_price > 0:
        prev_fmt = format_currency_amount(previous_price, currency)
        lines.append(f"📉 <b>آخر سعر:</b> {prev_fmt}\n")
        lines.append("💡 السعر الحالي أعلى من آخر سعر، لكنه ما زال أقل بكثير من السعر المرجعي.")
    else:
        lines.append("")

    if product_url:
        lines.extend(["\n🔗 <b>شوف العرض:</b>", product_url])

    return "\n".join(lines)


def format_restock_message(
    *,
    title: str,
    current_price: float,
    previous_price: float,
    reference_price: float | None = None,
    currency: str = "EGP",
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
) -> str:
    savings = previous_price - current_price
    drop_pct = (savings / previous_price * 100.0) if previous_price > 0 else 0.0

    curr_fmt = format_currency_amount(current_price, currency)
    prev_fmt = format_currency_amount(previous_price, currency)
    savings_fmt = format_currency_amount(savings, currency)

    lines = [
        "🔄 <b>المنتج رجع متاح!</b>\n",
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>السعر الحالي:</b> {curr_fmt}",
    ]
    if reference_price is not None and reference_price > 0:
        ref_fmt = format_currency_amount(reference_price, currency)
        ref_pct = ((reference_price - current_price) / reference_price * 100.0)
        lines.append(f"📊 <b>السعر المرجعي:</b> {ref_fmt}")
        lines.append(f"📉 <b>أقل من المرجع بـ</b> {ref_pct:.1f}%")

    if previous_price > 0:
        lines.append(f"📉 <b>آخر سعر قبل نفاد المخزون:</b> {prev_fmt}")

    if savings > 0:
        lines.extend([
            f"💵 <b>وفرت:</b> {savings_fmt}",
            f"📊 <b>انخفاض:</b> {drop_pct:.1f}%\n",
        ])
    else:
        lines.append("")

    if stats and stats.get("has_data") and stats.get("is_lowest"):
        lines.append("🏆 <b>أقل سعر مسجل حتى الآن!</b>")

    if product_url:
        lines.extend(["\n🔗 <b>اطلبه من هنا:</b>", product_url])

    return "\n".join(lines)


def format_resale_price_drop_message(
    *,
    title: str,
    current_price: float,
    previous_price: float,
    currency: str = "EGP",
    seller_condition: str | None = None,
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
) -> str:
    from telegram_publisher import format_resale_condition_arabic
    savings = previous_price - current_price
    drop_pct = (savings / previous_price * 100.0) if previous_price > 0 else 0.0

    curr_fmt = format_currency_amount(current_price, currency)
    prev_fmt = format_currency_amount(previous_price, currency)
    savings_fmt = format_currency_amount(savings, currency)
    cond_phrase = format_resale_condition_arabic(seller_condition)

    if drop_pct >= 10.0:
        header = "🔥 <b>Amazon Resale — تخفيض قوي!</b>\n"
    elif drop_pct >= 5.0:
        header = "♻️ <b>Amazon Resale — تخفيض ملحوظ!</b>\n"
    else:
        header = "♻️ <b>Amazon Resale — تحديث السعر</b>\n"

    lines = [
        header,
        f"<b>{cond_phrase}</b>",
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>السعر الحالي:</b> {curr_fmt}",
        f"📉 <b>كان:</b> {prev_fmt}",
        f"💵 <b>وفرت:</b> {savings_fmt}",
        f"📊 <b>انخفاض:</b> {drop_pct:.1f}%\n",
    ]

    if stats and stats.get("has_data"):
        lowest = stats["lowest_price"]
        highest = stats["highest_price"]
        avg = stats["average_price"]

        lowest_fmt = format_currency_amount(lowest, currency)
        highest_fmt = format_currency_amount(highest, currency)
        avg_fmt = format_currency_amount(avg, currency)

        lines.extend([
            "📈 <b>سجل Amazon Resale:</b>",
            f"• أعلى سعر مسجل: {highest_fmt}",
            f"• متوسط السعر: {avg_fmt}",
            f"• أقل سعر مسجل: {lowest_fmt}\n",
        ])

        if stats.get("is_lowest"):
            lines.append("🏷️ <b>تنويه:</b> هذا السعر هو أقل سعر مسجل بالمرصد حتى الآن.")

    if product_url:
        lines.extend(["\n🔗 <b>شوف العرض:</b>", product_url])

    return "\n".join(lines)


def format_resale_restock_message(
    *,
    title: str,
    current_price: float,
    previous_price: float,
    reference_price: float | None = None,
    currency: str = "EGP",
    seller_condition: str | None = None,
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
) -> str:
    from telegram_publisher import format_resale_condition_arabic
    curr_fmt = format_currency_amount(current_price, currency)
    cond_phrase = format_resale_condition_arabic(seller_condition)

    lines = [
        "♻️ <b>Amazon Resale رجع!</b>\n",
        f"<b>{cond_phrase}</b>",
        f"📦 <b>{short_title(title, 80)}</b>\n",
        f"💰 <b>السعر الحالي:</b> {curr_fmt}",
    ]
    if reference_price is not None and reference_price > 0:
        ref_fmt = format_currency_amount(reference_price, currency)
        ref_pct = ((reference_price - current_price) / reference_price * 100.0)
        lines.append(f"📊 <b>السعر المرجعي لـ Resale:</b> {ref_fmt}")
        lines.append(f"📉 <b>أقل من المرجع بـ</b> {ref_pct:.1f}%")

    if previous_price > 0:
        savings = previous_price - current_price
        drop_pct = (savings / previous_price * 100.0) if previous_price > 0 else 0.0
        prev_fmt = format_currency_amount(previous_price, currency)
        savings_fmt = format_currency_amount(savings, currency)

        lines.append(f"📉 <b>آخر سعر:</b> {prev_fmt}")
        if savings > 0:
            lines.extend([
                f"💵 <b>وفرت:</b> {savings_fmt}",
                f"📊 <b>انخفاض:</b> {drop_pct:.1f}%\n",
            ])
        else:
            lines.append("")
    else:
        lines.append("")

    if stats and stats.get("has_data") and stats.get("is_lowest"):
        lines.append("🏷️ <b>تنويه:</b> هذا السعر هو أقل سعر مسجل بالمرصد حتى الآن.")

    if product_url:
        lines.extend(["\n🔗 <b>شوف العرض:</b>", product_url])

    return "\n".join(lines)
