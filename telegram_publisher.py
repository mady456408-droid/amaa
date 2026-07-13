import asyncio
import logging
from typing import Any

import httpx
from telegram import Bot, Message
from telegram.error import NetworkError, TimedOut

from config import (
    PUBLISH_MAX_RETRIES,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)
from coupon_price import format_standard_price_line
from inline_buttons import short_product_name

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
) -> Message:
    last_error: Exception | None = None

    for attempt in range(1, PUBLISH_MAX_RETRIES + 1):
        logger.info("PUBLISH ATTEMPT %s", attempt)
        try:
            with open(photo_path, "rb") as photo:
                msg = await bot.send_photo(
                    chat_id=channel_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=reply_markup,
                    read_timeout=TELEGRAM_READ_TIMEOUT,
                    write_timeout=TELEGRAM_WRITE_TIMEOUT,
                )
            logger.info("PUBLISH SUCCESS")
            return msg
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
    parse_mode: str = "HTML",
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
        parse_mode: Parse mode for text message (HTML/Markdown)

    Returns:
        The photo message object
    """
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
            bot, channel_id, photo_path, short_caption, reply_markup
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
            bot, channel_id, photo_path, caption, reply_markup
        )
