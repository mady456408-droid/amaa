import asyncio
import logging
from typing import Any

import httpx
from telegram import Bot, Message
from telegram.error import BadRequest, NetworkError, TimedOut

from config import (
    PUBLISH_MAX_RETRIES,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)
from coupon_price import format_standard_price_line
from inline_buttons import short_product_name

import re

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = (2, 4)
SAFE_CAPTION_LENGTH = 900

RETRYABLE_ERRORS = (
    TimedOut,
    NetworkError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)


def strip_html_tags(text: str) -> str:
    """Strip HTML tags (e.g. <b>, </b>, <i>, </i>, <a>, </a>) from text."""
    if not text:
        return text
    return re.sub(r'</?[a-zA-Z][^>]*>', '', text)


def build_caption(
    title: str,
    price: str,
    clean_url: str,
    coupon: str | None = None,
    coupon_kwargs: dict | None = None,
) -> str:
    ck = coupon_kwargs or {}
    lines = [
        f"📦 {title}",
        "",
        format_standard_price_line(
            price, coupon, debug_path="build_caption", **ck
        ),
    ]
    lines.extend(["", f"🔗 {clean_url}"])
    return "\n".join(lines)


def format_resale_condition_arabic(seller_condition: str | None = None) -> str:
    """
    Format exact Amazon condition to natural Arabic for Amazon Resale posts.
    
    Preserves exact condition mappings:
    - Used - Like New -> "مستعمل - كالجديد"
    - Used - Very Good -> "مستعمل - جيد جدًا"
    - Used - Good -> "مستعمل - جيد"
    - Used - Acceptable -> "مستعمل - مقبول"
    
    If condition is unknown or empty:
    - "♻️ المنتج من Amazon Resale وهو مستعمل"
    """
    if not seller_condition:
        return "♻️ المنتج من Amazon Resale وهو مستعمل"
    
    cond_raw = seller_condition.strip()
    cond_lower = cond_raw.lower()
    
    if "like new" in cond_lower or "كالجديد" in cond_lower:
        return "♻️ المنتج مستعمل - كالجديد"
    elif "very good" in cond_lower or "جيد جدا" in cond_lower or "جيد جدًا" in cond_lower:
        return "♻️ المنتج مستعمل - جيد جدًا"
    elif "acceptable" in cond_lower or "مقبول" in cond_lower:
        return "♻️ المنتج مستعمل - مقبول"
    elif "good" in cond_lower or "جيد" in cond_lower:
        return "♻️ المنتج مستعمل - جيد"
    elif "مستعمل" in cond_raw:
        return f"♻️ {cond_raw}"
    else:
        return "♻️ المنتج من Amazon Resale وهو مستعمل"


def build_resale_caption(
    title: str,
    price: str,
    resale_url: str,
    seller_condition: str | None = None,
) -> str:
    """Build plain-text caption for Amazon Resale posts with explicit Arabic used/pre-owned condition."""
    condition_phrase = format_resale_condition_arabic(seller_condition)
    lines = [
        "♻️ Amazon Resale",
        f"{condition_phrase}",
        "",
        f"📦 {title}",
        "",
        f"💰 بسعر {price}",
        "",
        "🔗 شوف العرض:",
        resale_url,
    ]
    return "\n".join(lines)


def build_overflow_caption(product_count: int = 1) -> str:
    """Build short caption for overflow mode."""
    return (
        "🔥 أفضل عروض اليوم\n\n"
        f"📦 يحتوي هذا المنشور على {product_count} منتجات.\n"
        "⬇️ التفاصيل الكاملة وروابط الشراء في الرسالة التالية."
    )


def build_compact_product_summary(products: list[dict[str, Any]]) -> str:
    """
    Build compact product summary for overflow mode.

    Args:
        products: List of product dicts with 'title', 'url', and optionally 'price'

    Returns:
        Compact product summary with short names and affiliate links
    """
    if not products:
        return "📦 No products in this post"

    lines = ["🛍 عرض على\n"]

    for idx, product in enumerate(products, 1):
        title = product.get("title", "")
        url = product.get("url", "")
        price = product.get("price")

        short_name = short_product_name(title)
        lines.append(f"{idx}️⃣ {short_name}")
        lines.append(f"🔗 {url}")

        if price and price != "Not found":
            lines.append(f"💰 {price}")

        lines.append("")  # Empty line between products

    return "\n".join(lines).strip()


async def publish_to_channel(
    bot: Bot,
    channel_id: int,
    photo_path: str,
    caption: str,
    reply_markup=None,
    parse_mode: str | None = None,
    publish_type: str = "PRODUCT",
    source: str = "database",
) -> Message:
    caption = strip_html_tags(caption)
    last_error: Exception | None = None

    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        logger.info("PUBLISH ATTEMPT %s", attempt)
        try:
            logger.info(
                "PUBLISH DESTINATION:\n"
                "  chat_id=%s\n"
                "  source=%s\n"
                "  publish_type=%s",
                channel_id,
                source,
                publish_type,
            )
            with open(photo_path, "rb") as photo:
                msg = await bot.send_photo(
                    chat_id=channel_id,
                    photo=photo,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                )
            logger.info("PUBLISH SUCCESS")
            return msg
        except BadRequest as exc:
            if "chat not found" in str(exc).lower():
                logger.error(
                    "NON-RETRYABLE ERROR: BadRequest: %s for chat_id=%s",
                    exc,
                    channel_id,
                )
            else:
                logger.exception("Publish failed (BadRequest non-retryable): %s", exc)
            raise exc
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            logger.warning("UPLOAD TIMEOUT: %s", exc)
            if attempt < PUBLISH_MAX_RETRIES:
                wait = RETRY_BACKOFF_SECONDS[attempt - 1]
                logger.info("RETRYING in %s seconds", wait)
                await asyncio.sleep(wait)
            else:
                logger.error(
                    "Publish failed after %s attempts", PUBLISH_MAX_RETRIES
                )
        except Exception as exc:
            logger.exception("Publish failed (non-retryable): %s", exc)
            raise

    if last_error:
        raise last_error
    raise RuntimeError("Publish failed")


async def publish_to_channel_with_overflow(
    bot: Bot,
    channel_id: int,
    photo_path: str,
    caption: str,
    reply_markup=None,
    products: list[dict[str, Any]] | None = None,
    parse_mode: str | None = None,
    publish_type: str = "PRODUCT",
    source: str = "database",
) -> Message:
    """
    Publish photo to channel with automatic caption overflow handling.

    If caption exceeds SAFE_CAPTION_LENGTH, splits into two messages:
    1. Photo with short overflow caption + inline keyboard
    2. Text message with compact product summary (no buttons)

    Args:
        bot: Telegram bot instance
        channel_id: Target channel ID
        photo_path: Path to photo file
        caption: Full caption to send
        reply_markup: Inline keyboard for photo message
        products: List of product dicts for compact summary in overflow mode
        parse_mode: Parse mode for text message (None for plain text)
        publish_type: Type of post being published (PRODUCT/CODE)
        source: Destination configuration source (database/env)

    Returns:
        The photo message object
    """
    caption = strip_html_tags(caption)
    caption_length = len(caption)
    overflow_triggered = caption_length > SAFE_CAPTION_LENGTH
    product_count = len(products) if products else 1

    if overflow_triggered:
        # Overflow mode
        short_caption = build_overflow_caption(product_count)
        photo_caption_length = len(short_caption)

        # Build compact product summary
        if products:
            message_text = build_compact_product_summary(products)
        else:
            message_text = caption  # Fallback to full caption if no products

        message_text = strip_html_tags(message_text)
        message_caption_length = len(message_text)

        logger.info(
            "CAPTION DEBUG:\n"
            "length=%d\n"
            "safe_limit=%d\n"
            "overflow_triggered=True\n"
            "product_count=%d\n"
            "photo_caption_length=%d\n"
            "message_caption_length=%d\n"
            "photo_caption_first_50=%s\n"
            "photo_caption_last_50=%s",
            caption_length,
            SAFE_CAPTION_LENGTH,
            product_count,
            photo_caption_length,
            message_caption_length,
            short_caption[:50],
            short_caption[-50:] if len(short_caption) >= 50 else short_caption,
        )

        logger.info(
            "CAPTION OVERFLOW: length=%d mode=split products=%d",
            caption_length,
            product_count,
        )

        # Send photo with short caption and inline keyboard
        photo_msg = await publish_to_channel(
            bot,
            channel_id,
            photo_path,
            short_caption,
            reply_markup,
            parse_mode=parse_mode,
            publish_type=publish_type,
            source=source,
        )

        # Send compact product summary as text message (no buttons)
        await bot.send_message(
            chat_id=channel_id,
            text=message_text,
            parse_mode=parse_mode,
            read_timeout=TELEGRAM_READ_TIMEOUT,
            write_timeout=TELEGRAM_WRITE_TIMEOUT,
        )

        return photo_msg
    else:
        # Normal mode
        photo_caption_length = caption_length
        message_caption_length = 0

        logger.info(
            "CAPTION DEBUG:\n"
            "length=%d\n"
            "safe_limit=%d\n"
            "overflow_triggered=False\n"
            "product_count=%d\n"
            "photo_caption_length=%d\n"
            "message_caption_length=0\n"
            "photo_caption_first_50=%s\n"
            "photo_caption_last_50=%s",
            caption_length,
            SAFE_CAPTION_LENGTH,
            product_count,
            photo_caption_length,
            caption[:50],
            caption[-50:] if len(caption) >= 50 else caption,
        )

        logger.info("CAPTION: length=%d mode=normal", caption_length)
        return await publish_to_channel(
            bot,
            channel_id,
            photo_path,
            caption,
            reply_markup,
            parse_mode=parse_mode,
            publish_type=publish_type,
            source=source,
        )

