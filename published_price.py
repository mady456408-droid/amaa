"""Helpers for published price storage and price-drop reporting."""

from __future__ import annotations

from datetime import datetime
from html import escape as html_escape
import logging
import os
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
    """Format numeric amount for price-drop reports (e.g. 14,999 جنيه)."""
    if abs(value - round(value)) < 0.01:
        amount = f"{int(round(value)):,}"
    else:
        amount = f"{value:,.2f}".rstrip("0").rstrip(".")
    curr_label = "جنيه" if currency in ("EGP", "جنيه", "") else currency
    return f"{amount} {curr_label}"


def format_savings(value: float, currency: str = "EGP") -> str:
    """Format savings with sign (e.g. -1,000 جنيه)."""
    if abs(value - round(value)) < 0.01:
        amount = f"{int(round(abs(value))):,}"
    else:
        amount = f"{abs(value):,.2f}".rstrip("0").rstrip(".")
    sign = "-" if value > 0 else "+"
    curr_label = "جنيه" if currency in ("EGP", "جنيه", "") else currency
    return f"{sign}{amount} {curr_label}"


def short_title(title: str, max_len: int = 60) -> str:
    text = re.sub(r"\s+", " ", (title or "").strip())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def drop_index_emoji(index: int) -> str:
    if 1 <= index <= len(_NUMBER_EMOJIS):
        return _NUMBER_EMOJIS[index - 1]
    return f"{index}."


def calculate_publish_recommendation(
    current_price: float,
    original_publishing_price: float | None,
    currency: str = "EGP",
) -> tuple[list[str], bool, float | None]:
    """Calculate the recommendation lines, whether it's recommended to publish, and difference.

    Returns:
        (header_lines: list[str], is_publish_recommended: bool, diff: float | None)
    """
    has_pub = original_publishing_price is not None and original_publishing_price > 0
    if has_pub and current_price > 0:
        if current_price < original_publishing_price:
            savings = original_publishing_price - current_price
            return [
                "🚨 <b>الحق انشره دلوقتي!</b>",
                f"💰 <b>وفر:</b> {format_currency_amount(savings, currency)}",
            ], True, savings
        elif abs(current_price - original_publishing_price) < 0.01:
            return [
                "🚨 <b>السعر رجع لسعر النشر!</b>",
            ], True, 0.0
        else:
            diff = current_price - original_publishing_price
            return [
                "⚠️ <b>السعر أعلى من سعر النشر</b>",
                f"📈 <b>أعلى بـ</b> {format_currency_amount(diff, currency)}",
            ], False, -diff
    else:
        return [
            "ℹ️ <b>سعر النشر الأصلي غير متوفر</b>",
            "⚠️ لا يمكن مقارنة السعر الحالي بسعر النشر",
        ], False, None


def format_detailed_price_drop_message(
    *,
    title: str,
    current_price: float,
    previous_price: float | None = None,
    currency: str = "EGP",
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
    coupon: str | None = None,
    seller: str | None = None,
    original_publishing_price: float | None = None,
    asin: str | None = None,
    availability: str | None = None,
) -> str:
    """Consolidated recommendation-first price alert formatter."""
    rec_lines, is_rec, diff = calculate_publish_recommendation(
        current_price=current_price,
        original_publishing_price=original_publishing_price,
        currency=currency,
    )

    lines: list[str] = list(rec_lines)
    lines.append("")

    curr_fmt = format_currency_amount(current_price, currency) if current_price > 0 else "غير متوفر"
    has_pub = original_publishing_price is not None and original_publishing_price > 0
    pub_fmt = format_currency_amount(original_publishing_price, currency) if has_pub else "غير متوفر"

    lines.append(f"💰 <b>السعر الحالي:</b> {curr_fmt}")
    lines.append(f"📌 <b>سعر النشر:</b> {pub_fmt}")

    if previous_price and previous_price > 0 and abs(previous_price - current_price) > 0.01:
        lines.append(f"🔄 <b>السعر السابق:</b> {format_currency_amount(previous_price, currency)}")

    if stats and stats.get("has_data"):
        lowest = stats.get("lowest_price")
        lowest_at = stats.get("lowest_recorded_at")
        if current_price > 0 and (lowest is None or current_price < lowest):
            lowest = current_price
        if lowest and lowest > 0:
            lines.append(f"📉 <b>أقل سعر مسجل:</b> {format_currency_amount(lowest, currency)}")
            if lowest_at:
                try:
                    dt = datetime.fromisoformat(lowest_at.replace("Z", "+00:00"))
                    lines.append(f"📅 <b>تاريخ أقل سعر:</b> {dt.strftime('%d/%m/%Y')}")
                except Exception:
                    lines.append(f"📅 <b>تاريخ أقل سعر:</b> {lowest_at[:10]}")

    lines.append("")
    lines.append(availability or "📦 متوفر — أكتر من قطعة")
    lines.append("")
    lines.append(f"📦 <b>{html_escape(short_title(title, 80))}</b>")
    if asin:
        lines.append(f"🔗 ASIN: <code>{asin}</code>")
    elif product_url:
        lines.append(f"🔗 {product_url}")

    return "\n".join(lines)


ARABIC_MONTHS = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}


def _clean_emoji(text: str) -> str:
    """Remove emoji symbols for TTF font rendering without missing glyph warnings."""
    return re.sub(r"[\U00010000-\U0010ffff]", "", text).strip()


def ar_text(text: str | None) -> str:
    """Reshape Arabic text and apply BiDi algorithm for Matplotlib rendering."""
    if not text:
        return ""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display

        clean = _clean_emoji(str(text))
        reshaped = arabic_reshaper.reshape(clean)
        return get_display(reshaped)
    except Exception:
        return str(text)


def _discover_arabic_fonts() -> tuple[Any, Any, str]:
    """Discover available Arabic fonts from bundled fonts/ directory or system fallbacks."""
    import matplotlib.font_manager as fm

    base_dir = os.path.dirname(os.path.abspath(__file__))
    bundled_fonts = [
        ("NotoSansArabic-Regular.ttf", "NotoSansArabic-Bold.ttf", "Noto Sans Arabic"),
        ("NotoSansArabic.ttf", "NotoSansArabic-Bold.ttf", "Noto Sans Arabic"),
        ("Cairo.ttf", "Cairo.ttf", "Cairo"),
    ]
    for reg, bold, font_name in bundled_fonts:
        reg_path = os.path.join(base_dir, "fonts", reg)
        bold_path = os.path.join(base_dir, "fonts", bold)
        if os.path.exists(reg_path) and os.path.exists(bold_path):
            try:
                fm.fontManager.addfont(reg_path)
                fm.fontManager.addfont(bold_path)
                reg_prop = fm.FontProperties(fname=reg_path)
                bold_prop = fm.FontProperties(fname=bold_path)
                return reg_prop, bold_prop, font_name
            except Exception:
                pass

    for sys_font in ["Noto Sans Arabic", "Noto Kufi Arabic", "Cairo", "DejaVu Sans"]:
        try:
            prop = fm.FontProperties(family=sys_font)
            return prop, prop, sys_font
        except Exception:
            pass

    fallback = fm.FontProperties(family="DejaVu Sans")
    return fallback, fallback, "DejaVu Sans"


def generate_price_chart_image(
    asin: str,
    title: str,
    records: list[dict[str, Any]],
    original_publishing_price: float | None = None,
) -> str | None:
    """
    Generate a polished, professional Arabic RTL price-history chart image for Telegram.

    Produces a high-resolution (1760x1200 px) dark luxury e-commerce card visualizer
    without relying on emoji symbols in TTF fonts. Preserves all historical price points
    and respects original_publishing_price reference lines and recommendation banners.
    """
    if not records or len(records) < 2:
        return None

    try:
        import os
        import re
        import tempfile
        import textwrap
        from datetime import datetime
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.ticker as ticker

        reg_prop, bold_prop, font_name = _discover_arabic_fonts()

        sorted_recs = sorted(records, key=lambda r: r.get("recorded_at") or "")
        dates_str = []
        prices = []

        pub_price_from_rec = None
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

            if r.get("original_published_price_value") and float(r["original_published_price_value"]) > 0:
                pub_price_from_rec = float(r["original_published_price_value"])

            try:
                dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
                m_name = ARABIC_MONTHS.get(dt.month, dt.strftime("%b"))
                dates_str.append(f"{dt.day} {m_name}")
            except Exception:
                dates_str.append(raw_dt[:10] if raw_dt else "N/A")

            prices.append(p_val)

        if not prices or len(prices) < 2:
            return None

        pub_price = original_publishing_price if (original_publishing_price is not None and original_publishing_price > 0) else pub_price_from_rec

        # 1760 x 1200 px at 160 DPI
        fig, ax = plt.subplots(figsize=(11, 7.5), dpi=160)
        fig.patch.set_facecolor("#0B1120")

        # 1. Main Title
        title_ar = ar_text("سجل أسعار المنتج")
        fig.text(0.5, 0.935, title_ar, fontproperties=bold_prop, fontsize=18, color="#F8FAFC", ha="center")

        # 2. Subtitle & ASIN/Model Separation
        clean_title = (title or "").strip()
        # Clean title for display (remove appended Model/ASIN substring if present)
        display_title = re.sub(r"(?i)\b(?:Model|Modell?)\s*[:\-]?\s*[A-Z0-9]{10}\b", "", clean_title)
        display_title = re.sub(r"(?i)\s*[\-\|]?\s*B0[A-Z0-9]{8}\b", "", display_title).strip()
        if not display_title:
            display_title = clean_title

        wrapped_title = textwrap.fill(display_title, width=58)
        sub_lines = wrapped_title.split("\n")
        if len(sub_lines) > 2:
            wrapped_title = "\n".join(sub_lines[:2]) + "..."
        
        fig.text(0.5, 0.875, ar_text(wrapped_title), fontproperties=reg_prop, fontsize=10.5, color="#94A3B8", ha="center", va="top")
        
        tech_line = f"Model: {asin.upper()}  |  ASIN: {asin.upper()}" if asin else "ASIN: N/A"
        fig.text(0.5, 0.812, tech_line, fontproperties=reg_prop, fontsize=9, color="#64748B", ha="center")

        # 3. Summary Cards (Current, Lowest, Publishing)
        curr_p = prices[-1]
        min_p = min(prices)

        if pub_price and pub_price > 0:
            cards = [
                ("السعر الحالي", f"{curr_p:,.0f} جنيه", "#38BDF8"),
                ("أقل سعر", f"{min_p:,.0f} جنيه", "#22C55E"),
                ("سعر النشر", f"{pub_price:,.0f} جنيه", "#F59E0B"),
            ]
            x_centers = [0.24, 0.50, 0.76]
        else:
            cards = [
                ("السعر الحالي", f"{curr_p:,.0f} جنيه", "#38BDF8"),
                ("أقل سعر", f"{min_p:,.0f} جنيه", "#22C55E"),
            ]
            x_centers = [0.34, 0.66]

        card_y = 0.73

        for (label_text, val_text, val_color), x_c in zip(cards, x_centers):
            card_str = f"{ar_text(label_text)}\n{ar_text(val_text)}"
            fig.text(
                x_c,
                card_y,
                card_str,
                fontproperties=bold_prop,
                fontsize=11,
                color=val_color,
                ha="center",
                va="center",
                bbox=dict(
                    boxstyle="round,pad=0.55,rounding_size=0.35",
                    facecolor="#172033",
                    edgecolor="#263449",
                    linewidth=1.2,
                ),
            )

        # 4. Main Plot Canvas & Dynamic Y-Axis Range
        ax.set_position([0.10, 0.19, 0.80, 0.45])
        ax.set_facecolor("#111827")

        all_vals = list(prices)
        if pub_price and pub_price > 0:
            all_vals.append(pub_price)

        min_val = min(all_vals)
        max_val = max(all_vals)
        val_span = max_val - min_val

        if val_span <= 0:
            padding = max_val * 0.12 if max_val > 0 else 20.0
        else:
            padding = max(val_span * 0.20, 25.0)

        y_min = max(0.0, min_val - padding)
        y_max = max_val + padding
        ax.set_ylim(y_min, y_max)

        x_indices = list(range(len(prices)))
        ax.plot(x_indices, prices, color="#38BDF8", linewidth=2.8, marker="o", markersize=5.5, zorder=4)
        ax.fill_between(x_indices, prices, y_min, color="#38BDF8", alpha=0.10)

        # Reference line for Publishing Price
        if pub_price and pub_price > 0:
            ax.axhline(
                y=pub_price,
                color="#F59E0B",
                linestyle="--",
                linewidth=1.8,
                zorder=3,
            )
            ax.text(
                0.98,
                pub_price,
                ar_text(f"سعر النشر: {pub_price:,.0f} جنيه"),
                transform=ax.get_yaxis_transform(),
                fontproperties=bold_prop,
                fontsize=8.8,
                color="#F59E0B",
                ha="right",
                va="bottom",
                zorder=5,
                bbox=dict(boxstyle="round,pad=0.25", facecolor="#0B1120", edgecolor="#F59E0B", linewidth=0.8, alpha=0.9),
            )

        # Minimum and Current Price Markers & Intelligent Annotations
        min_idx = prices.index(min_p)
        curr_idx = len(prices) - 1

        if min_idx == curr_idx:
            # Combined single marker callout
            ax.scatter(min_idx, prices[min_idx], color="#22C55E", s=150, zorder=6, edgecolors="#FFFFFF", linewidth=2)
            ax.annotate(
                ar_text(f"أقل سعر وحالي: {min_p:,.0f} جنيه"),
                (min_idx, prices[min_idx]),
                xytext=(-12 if min_idx >= len(prices) - 2 else 0, 18),
                textcoords="offset points",
                fontproperties=bold_prop,
                fontsize=9.2,
                color="#22C55E",
                ha="right" if min_idx >= len(prices) - 2 else "center",
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.35", facecolor="#0B1120", edgecolor="#22C55E", linewidth=1.4, alpha=0.95),
            )
        else:
            # Distinct Minimum Price Marker
            is_min_right = (min_idx >= len(prices) - 2)
            is_min_left = (min_idx == 0)
            is_min_bottom = (prices[min_idx] < min_val + val_span * 0.3) if val_span > 0 else True
            
            y_off_min = 18 if is_min_bottom else -22
            x_off_min = 12 if is_min_left else (-12 if is_min_right else 0)
            ha_align_min = "left" if is_min_left else ("right" if is_min_right else "center")

            ax.scatter(min_idx, prices[min_idx], color="#22C55E", s=135, zorder=6, edgecolors="#FFFFFF", linewidth=2)
            ax.annotate(
                ar_text(f"أقل سعر: {min_p:,.0f} جنيه"),
                (min_idx, prices[min_idx]),
                xytext=(x_off_min, y_off_min),
                textcoords="offset points",
                fontproperties=bold_prop,
                fontsize=8.8,
                color="#22C55E",
                ha=ha_align_min,
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#0B1120", edgecolor="#22C55E", linewidth=1.2, alpha=0.95),
            )

            # Distinct Current Price Marker
            is_curr_right = (curr_idx >= len(prices) - 2)
            is_curr_top = (prices[curr_idx] > min_val + val_span * 0.7) if val_span > 0 else True

            y_off_curr = -22 if is_curr_top else 18
            x_off_curr = -12 if is_curr_right else 0
            ha_align_curr = "right" if is_curr_right else "center"

            ax.scatter(curr_idx, prices[curr_idx], color="#38BDF8", s=135, zorder=6, edgecolors="#FFFFFF", linewidth=2)
            ax.annotate(
                ar_text(f"السعر الحالي: {curr_p:,.0f} جنيه"),
                (curr_idx, prices[curr_idx]),
                xytext=(x_off_curr, y_off_curr),
                textcoords="offset points",
                fontproperties=bold_prop,
                fontsize=8.8,
                color="#38BDF8",
                ha=ha_align_curr,
                zorder=7,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="#0B1120", edgecolor="#38BDF8", linewidth=1.2, alpha=0.95),
            )

        # X-Axis Ticks (Intelligent Spacing)
        if len(prices) <= 7:
            tick_indices = list(range(len(prices)))
        else:
            step = (len(prices) - 1) / 6.0
            tick_indices = sorted(list(set([int(round(i * step)) for i in range(7)])))

        ax.set_xticks(tick_indices)
        ax.set_xticklabels([ar_text(dates_str[i]) for i in tick_indices], fontproperties=reg_prop, fontsize=9.2, color="#94A3B8")

        # Y-Axis Formatting (Dynamic Tick Spacing)
        ax.yaxis.set_major_locator(ticker.MaxNLocator(nbins=5, steps=[1, 2, 5, 10]))
        ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda y, p: f"{y:,.0f} " + ar_text("جنيه")))
        for label in ax.get_yticklabels():
            label.set_fontproperties(reg_prop)
            label.set_color("#94A3B8")
            label.set_fontsize(9.2)

        ax.grid(True, color="#263449", linestyle="--", alpha=0.40)
        for spine in ax.spines.values():
            spine.set_color("#263449")

        # 5. Recommendation Indicator Banner at Bottom
        if pub_price and pub_price > 0:
            if curr_p < pub_price:
                savings = pub_price - curr_p
                rec_lines = ["الحق انشره دلوقتي!", f"وفر {savings:,.0f} جنيه"]
                rec_color = "#22C55E"
            elif curr_p == pub_price:
                rec_lines = ["السعر رجع لسعر النشر!"]
                rec_color = "#22C55E"
            else:
                diff = curr_p - pub_price
                rec_lines = ["السعر أعلى من سعر النشر", f"أعلى بـ {diff:,.0f} جنيه"]
                rec_color = "#EF4444"

            rec_text_ar = "\n".join(ar_text(l) for l in rec_lines)

            fig.text(
                0.5,
                0.072,
                rec_text_ar,
                fontproperties=bold_prop,
                fontsize=11,
                color=rec_color,
                ha="center",
                va="center",
                bbox=dict(
                    boxstyle="round,pad=0.6,rounding_size=0.4",
                    facecolor="#111827",
                    edgecolor=rec_color,
                    linewidth=1.8,
                ),
            )

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
    previous_price: float | None = None,
    currency: str = "EGP",
    seller_condition: str | None = None,
    stats: dict[str, Any] | None = None,
    product_url: str | None = None,
    original_publishing_price: float | None = None,
    asin: str | None = None,
    availability: str | None = None,
) -> str:
    """Consolidated recommendation-first Amazon Resale price alert formatter."""
    from telegram_publisher import format_resale_condition_arabic
    avail_str = availability
    if not avail_str:
        if seller_condition:
            cond_phrase = format_resale_condition_arabic(seller_condition)
            avail_str = f"📦 Available — Amazon Resale ({cond_phrase})"
        else:
            avail_str = "📦 Available — Amazon Resale"

    return format_detailed_price_drop_message(
        title=title,
        current_price=current_price,
        previous_price=previous_price,
        currency=currency,
        stats=stats,
        product_url=product_url,
        original_publishing_price=original_publishing_price,
        asin=asin,
        availability=avail_str,
    )


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
