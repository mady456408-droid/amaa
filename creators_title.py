"""Arabic title resolution for Creators API product card framing."""

from __future__ import annotations

import asyncio
import logging
import os
import re

from config import AI_CAPTION_TIMEOUT, AI_MODEL, AI_PROVIDER

logger = logging.getLogger(__name__)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

_TRANSLATION_PROMPT = """Translate this Amazon Egypt product title to Arabic for an e-commerce product card.

Rules:
- Output ONLY the translated title on a single line. No quotes, labels, or explanation.
- Preserve brand names in Latin script (Samsung, Apple, Xiaomi, etc.).
- Preserve model names (Galaxy S25 Ultra, Redmi Note 15, etc.).
- Preserve capacities and specs (256GB, 12GB RAM, etc.).
- Preserve color names in Latin script when they are product color names (Titanium Silverblue, Black, etc.).
- Preserve numbers and technical specifications exactly.
- Do NOT translate ASINs or product codes.
- Use natural Arabic suitable for an Egyptian e-commerce listing.

Example input:
Samsung Galaxy S25 Ultra AI Phone, 256GB Storage, 12GB RAM, Titanium Silverblue

Example output:
هاتف سامسونج Galaxy S25 Ultra المزود بالذكاء الاصطناعي، سعة تخزين 256 جيجابايت، ذاكرة RAM ‏12 جيجابايت، لون Titanium Silverblue

Input title:
{title}
"""


def contains_arabic(text: str) -> bool:
    return any(
        "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" for ch in text
    )


def _clean_model_output(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:\w*\n)?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    cleaned = cleaned.strip().strip('"').strip("'")
    return cleaned.strip()


def _sync_groq_translate(prompt: str) -> str:
    from groq import Groq

    client = Groq(api_key=GROQ_API_KEY)
    response = client.chat.completions.create(
        model=AI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_completion_tokens=512,
        temperature=0.2,
    )
    if not response.choices or not response.choices[0].message.content:
        raise ValueError("Empty Groq response")
    return _clean_model_output(response.choices[0].message.content)


async def translate_product_title(title: str) -> str | None:
    """Translate an English product title to Arabic; return None on failure."""
    if not title or title.strip() == "Not found":
        return None
    if contains_arabic(title):
        return title.strip()

    if AI_PROVIDER != "groq":
        logger.warning("Title translation skipped — AI_PROVIDER=%s unsupported", AI_PROVIDER)
        return None
    if not GROQ_API_KEY:
        logger.warning("Title translation skipped — GROQ_API_KEY missing")
        return None

    prompt = _TRANSLATION_PROMPT.format(title=title.strip())
    try:
        translated = await asyncio.wait_for(
            asyncio.to_thread(_sync_groq_translate, prompt),
            timeout=AI_CAPTION_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Title translation timed out after %ss", AI_CAPTION_TIMEOUT)
        return None
    except Exception:
        logger.exception("Title translation failed")
        return None

    if not translated or not contains_arabic(translated):
        logger.warning("Title translation returned no usable Arabic text")
        return None
    return translated


async def resolve_frame_title(asin: str, title: str, db=None) -> str:
    """
    Resolve the title shown on the Creators product card frame.

    Priority: existing Arabic title -> cached translation -> AI translation -> original.
    """
    if not title or title.strip() == "Not found":
        return title

    normalized = title.strip()
    if contains_arabic(normalized):
        return normalized

    if db is not None:
        cached = db.get_creators_title_cache(asin)
        if cached and cached["english_title"] == normalized:
            logger.info("FRAME TITLE CACHE HIT asin=%s", asin.upper())
            return cached["arabic_title"]

    translated = await translate_product_title(normalized)
    if translated:
        if db is not None:
            db.set_creators_title_cache(asin, normalized, translated)
        logger.info("FRAME TITLE TRANSLATED asin=%s", asin.upper())
        return translated

    logger.info("FRAME TITLE FALLBACK asin=%s (English)", asin.upper())
    return normalized
