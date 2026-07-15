"""AI caption rewrite via Groq (async-safe, fallback on failure)."""

import asyncio
import logging
import os
import re

from config import (
    AI_CAPTION_ENABLED,
    AI_CAPTION_TIMEOUT,
    AI_MODEL,
    AI_PROVIDER,
    AMAZON_DOMAIN,
)
from coupon_price import (
    apply_coupon_to_price,
    coupon_apply_kwargs_from_product,
    effective_coupon_for_caption,
    format_arabic_price_line,
    normalize_caption_price_line,
)
from telegram_publisher import build_caption

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

MODE_OFF = "off"
MODE_CONSERVATIVE = "conservative"
MODE_MARKETING = "marketing"
MODE_ARABIC = "arabic_translate"
MODE_CUSTOM = "custom"
MODE_FIXED_TEMPLATE = "fixed_template"

VALID_MODES = {
    MODE_OFF,
    MODE_CONSERVATIVE,
    MODE_MARKETING,
    MODE_ARABIC,
    MODE_CUSTOM,
    MODE_FIXED_TEMPLATE,
}

_SAFETY_RULES = """
FACT SAFETY — follow exactly:
- Use ONLY facts from the product title, price, and marketplace provided.
- DO NOT invent or guess: storage, RAM, colors, wattage, features, discounts, or specs not in the title.
- DO NOT mention features, specs, or benefits that are not clearly present in the product title.
- If uncertain about any detail, omit it entirely.
- Shorten ugly long English titles into a clean short Arabic product name on the 📦 line only.
- Do not use markdown code blocks.
"""

_FORMATTING_RULES = """
FORMATTING RULES — follow exactly:
- Write the caption in Arabic (Egyptian-friendly).
- Put EXACTLY one blank line between each section block (never two blank lines in a row).
- No blank line before the first line; no blank line after the last line.
- No trailing spaces on any line.
- Use Arabic-friendly punctuation; avoid ugly English punctuation in Arabic text.
- Keep the 📦 product name short (one concise line, not a long sentence).
- Price line must be exactly: 💰 السعر: {formatted price}
- Format price naturally in Arabic: number then "جنيه" (e.g. 25,900 جنيه).
  Convert messy input like "جنيه‎25,900.00‎", "EGP 25,999", or "25,999.00" → "25,900 جنيه" / "25,999 جنيه".
  Keep the same numeric amount from the input; only fix formatting and currency wording.
- Link line must be exactly: 🔗 {url} with the full product URL copied verbatim (no shortening).
- ✅ bullets: 0 to 4 lines; each line starts with "✅ " then a very short fact from the title only.
  If the title has no clear separate features/specs, omit ALL ✅ lines completely.
- Total caption under 900 characters.
"""


def _caption_template(mode: str, custom_prompt: str | None) -> str:
    """Return the required output skeleton for the given mode."""
    if mode == MODE_CONSERVATIVE:
        return """
REQUIRED OUTPUT STRUCTURE (Conservative — copy layout exactly):

📦 {clean short Arabic product name}

✅ feature 1
✅ feature 2
(optional ✅ lines only if clearly in title; otherwise omit entire ✅ block)

💰 السعر: {formatted price}

🔗 {exact amazon url}

Rules for this mode:
- NO headline line; NO "اطلب الآن" line.
- No marketing hype; facts only.
- One blank line between each block above.
"""
    if mode == MODE_ARABIC:
        return """
REQUIRED OUTPUT STRUCTURE (Arabic Translate — copy layout exactly):

📦 {clean short Arabic product name}

✅ feature 1
(optional ✅ lines only if clearly in title; otherwise omit entire ✅ block)

💰 السعر: {formatted price}

🔗 {exact amazon url}

Rules for this mode:
- NO headline; NO call-to-action line.
- Plain accurate Arabic translation; no marketing exaggeration.
- One blank line between each block above.
"""
    if mode == MODE_CUSTOM and custom_prompt:
        return f"""
REQUIRED OUTPUT STRUCTURE (Custom Brand Tone — copy layout exactly):

{{custom opening line per brand instructions below}}

📦 {{clean short Arabic product name}}

✅ feature 1
✅ feature 2
(optional ✅ feature 3–4 only if clearly in title; otherwise omit entire ✅ block)

💰 السعر: {{formatted price}}

اطلب الآن 👇
🔗 {{exact amazon url}}

Brand instructions for the FIRST line only (replace the placeholder opening line):
{custom_prompt.strip()}

Keep all other sections and blank lines exactly as shown.
One blank line between each block.
"""
    if mode == MODE_MARKETING:
        return """
REQUIRED OUTPUT STRUCTURE (Marketing — copy layout exactly):

🔥 لقطة اليوم

📦 {clean short Arabic product name}

✅ feature 1
✅ feature 2
✅ feature 3
✅ feature 4
(Use 0–4 ✅ lines: only facts clearly in the title; omit unused slots and omit block if none)

💰 السعر: {formatted price}

اطلب الآن 👇
🔗 {exact amazon url}

Rules for this mode:
- Egyptian marketing tone; engaging but factual.
- First line MUST be exactly: 🔥 لقطة اليوم
- One blank line between each block above; no extra blank lines.
"""
    return _caption_template(MODE_MARKETING, None)


def _mode_instructions(mode: str, custom_prompt: str | None) -> str:
    template = _caption_template(mode, custom_prompt)
    if mode == MODE_CONSERVATIVE:
        return "Mode: Conservative.\n" + template
    if mode == MODE_MARKETING:
        return "Mode: Marketing.\n" + template
    if mode == MODE_ARABIC:
        return "Mode: Arabic Translate.\n" + template
    if mode == MODE_CUSTOM and custom_prompt:
        return "Mode: Custom Brand Tone.\n" + template
    return "Mode: Marketing (default).\n" + template


def _build_prompt(
    title: str,
    price: str,
    marketplace: str,
    clean_url: str,
    mode: str,
    custom_prompt: str | None,
    coupon: str | None = None,
    coupon_kwargs: dict | None = None,
) -> str:
    instructions = _mode_instructions(mode, custom_prompt)
    ck = coupon_kwargs or {}
    price_result = apply_coupon_to_price(price, coupon, **ck)
    effective_coupon = effective_coupon_for_caption(coupon, price_result)
    coupon_block = ""
    if effective_coupon:
        price_line_hint = format_arabic_price_line(
            price, effective_coupon, debug_path="_build_prompt", **ck
        )
        coupon_block = (
            "Price line (use this exact format — coupon already applied to price, "
            "do NOT add a separate 🎟 coupon line):\n"
            f"{price_line_hint}\n"
            f"Original scraped price (reference only): {price}\n"
        )
    return (
        "You write Telegram product post captions in Arabic for Amazon Egypt.\n"
        f"{_SAFETY_RULES}\n"
        f"{_FORMATTING_RULES}\n"
        f"{instructions}\n"
        "Input data (use only this):\n"
        f"Product title: {title}\n"
        f"Price (raw scraped): {price}\n"
        f"{coupon_block}"
        f"Marketplace: {marketplace}\n"
        f"Product URL (paste verbatim on 🔗 line): {clean_url}\n\n"
        "Output ONLY the final caption text with the exact structure and spacing described above. "
        "No explanation, no quotes, no preamble."
    )


def _sync_groq_generate(prompt: str) -> str:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=1024,
        temperature=0.5,
    )
    if not response.choices or not response.choices[0].message.content:
        raise ValueError("Empty Groq response")
    return response.choices[0].message.content.strip()


def _ensure_url_in_caption(caption: str, clean_url: str) -> str:
    if clean_url in caption:
        return caption
    return f"{caption.rstrip()}\n\n🔗 {clean_url}"


async def rewrite_caption(
    title: str,
    price: str,
    marketplace: str,
    clean_url: str,
    mode: str,
    custom_prompt: str | None = None,
    coupon: str | None = None,
    coupon_kwargs: dict | None = None,
) -> str:
    """
    Rewrite caption using AI, or return standard caption when off/disabled/failed.
    """
    ck = coupon_kwargs or {}
    fallback = build_caption(title, price, clean_url, coupon=coupon, coupon_kwargs=ck)
    mode = (mode or MODE_OFF).strip().lower()

    if mode not in VALID_MODES:
        mode = MODE_OFF

    if mode == MODE_OFF or not AI_CAPTION_ENABLED:
        return fallback

    if mode == MODE_FIXED_TEMPLATE:
        price_result = apply_coupon_to_price(price, coupon, **ck)
        effective = effective_coupon_for_caption(coupon, price_result)
        parts = [
            f" عرض على {title}",
            "",
            format_arabic_price_line(
                price,
                effective,
                debug_path="rewrite_caption:fixed_template",
                **ck,
            ),
        ]
        parts.extend(["", "🔗 لينك العرض:", clean_url])
        return "\n".join(parts).strip()

    if AI_PROVIDER != "groq":
        logger.warning("AI_PROVIDER %s not supported — fallback", AI_PROVIDER)
        logger.info("FALLBACK TO NORMAL CAPTION")
        return fallback

    if not GROQ_API_KEY:
        logger.warning("GROQ_API_KEY missing — fallback")
        logger.info("FALLBACK TO NORMAL CAPTION")
        return fallback

    if mode == MODE_CUSTOM and not (custom_prompt or "").strip():
        logger.warning("Custom mode without prompt — fallback")
        logger.info("FALLBACK TO NORMAL CAPTION")
        return fallback

    mode_label = {
        MODE_CONSERVATIVE: "Conservative",
        MODE_MARKETING: "Marketing",
        MODE_ARABIC: "Arabic Translate",
        MODE_CUSTOM: "Custom Brand Tone",
        MODE_FIXED_TEMPLATE: "Fixed Template",
    }.get(mode, mode)
    logger.info("AI CAPTION MODE: %s", mode_label)
    logger.info("AI REWRITE START")

    prompt = _build_prompt(
        title, price, marketplace, clean_url, mode, custom_prompt, coupon, ck
    )

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_sync_groq_generate, prompt),
            timeout=AI_CAPTION_TIMEOUT,
        )
        if not text:
            raise ValueError("Empty caption from model")
        caption = _ensure_url_in_caption(text, clean_url)
        caption = normalize_caption_price_line(
            caption,
            price,
            coupon,
            debug_path=f"rewrite_caption:ai:{mode}",
            **ck,
        )
        if len(caption) > 1024:
            caption = caption[:1020] + "…"
        logger.info("AI REWRITE SUCCESS")
        return caption
    except asyncio.TimeoutError:
        logger.warning("AI REWRITE FAILED: timeout after %ss", AI_CAPTION_TIMEOUT)
    except Exception:
        logger.exception("AI REWRITE FAILED")
    logger.info("FALLBACK TO NORMAL CAPTION")
    return fallback


async def build_product_caption(
    db,
    title: str,
    price: str,
    clean_url: str,
    marketplace: str | None = None,
    coupon: str | None = None,
    product: dict | None = None,
) -> str:
    """Build caption using DB AI mode settings."""
    marketplace = marketplace or AMAZON_DOMAIN
    mode = db.get_ai_caption_mode()
    custom = db.get_ai_custom_prompt()
    use_coupon = coupon if db.get_coupon_detection_enabled() else None
    coupon_kwargs = coupon_apply_kwargs_from_product(product)
    caption = await rewrite_caption(
        title,
        price,
        marketplace,
        clean_url,
        mode,
        custom,
        use_coupon,
        coupon_kwargs,
    )
    if use_coupon and price and price != "Not found":
        caption = normalize_caption_price_line(
            caption,
            price,
            use_coupon,
            debug_path=f"build_product_caption:{mode}",
            **coupon_kwargs,
        )
    return caption
