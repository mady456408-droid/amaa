"""Published price monitoring — batch Creators API checks and admin reports."""

from __future__ import annotations

import logging
import time
from html import escape as html_escape
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ai_caption import build_product_caption
from amazon_shortener import shorten_amazon_url
from config import ADMIN_USER_IDS, AMAZON_DOMAIN
from coupon_price import coupon_apply_kwargs_from_product, parse_price_number
from creators_api import PRICE_DROP_PROFILE, creators_api_configured, get_creators_client
from database import Database
from file_cleanup import cleanup_files
from inline_buttons import build_inline_keyboard
from link_resolver import build_clean_url
from product_fetcher import fetch_product, resolve_display_url
from published_price import (
    drop_index_emoji,
    extract_published_price_fields,
    format_currency_amount,
    format_savings,
    short_title,
)
from telegram_publisher import build_caption, publish_to_channel_with_overflow
from multi_publisher import publish_to_destinations
from upload_prep import to_jpeg_for_telegram

logger = logging.getLogger(__name__)

CB_REPUBLISH = "republish_drop:"
CB_REPUBLISH_CONFIRM = "republish_confirm:"
CB_VIEW_OLD_POST = "view_old_post:"

_MAX_PRODUCTS_PER_MESSAGE = 8
_TELEGRAM_TEXT_LIMIT = 4000


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def channel_post_url(channel_id: int, message_id: int) -> str:
    """Build t.me/c/ link for a channel post."""
    raw = str(channel_id)
    if raw.startswith("-100"):
        raw = raw[4:]
    elif raw.startswith("-"):
        raw = raw[1:]
    return f"https://t.me/c/{raw}/{message_id}"


def _format_drop_block(index: int, drop: dict[str, Any]) -> str:
    currency = drop.get("currency") or "EGP"
    published_display = drop.get("published_price") or format_currency_amount(
        drop["published_value"], currency
    )
    current_display = drop.get("current_price") or format_currency_amount(
        drop["current_value"], currency
    )
    savings = drop["published_value"] - drop["current_value"]
    return (
        f"{drop_index_emoji(index)} <b>{html_escape(short_title(drop['title']))}</b>\n\n"
        f"Published:\n{html_escape(published_display)}\n\n"
        f"Current:\n{html_escape(current_display)}\n\n"
        f"Difference:\n{html_escape(format_savings(savings, currency))}"
    )


def _build_drop_keyboard(drops: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for drop in drops:
        pid = drop["published_id"]
        channel_id = drop.get("destination_channel_id")
        message_id = drop.get("destination_message_id")
        row: list[InlineKeyboardButton] = [
            InlineKeyboardButton("📢 Republish", callback_data=f"{CB_REPUBLISH}{pid}"),
        ]
        if channel_id and message_id:
            row.append(
                InlineKeyboardButton(
                    "📝 View Old Post",
                    url=channel_post_url(channel_id, message_id),
                )
            )
        rows.append(row)
    return InlineKeyboardMarkup(rows)


def _build_republish_confirm_keyboard(published_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Yes, Republish",
                    callback_data=f"{CB_REPUBLISH_CONFIRM}{published_id}",
                ),
                InlineKeyboardButton("❌ Cancel", callback_data="republish_cancel"),
            ],
        ]
    )


def _chunk_drops(drops: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_len = 0
    for drop in drops:
        block = _format_drop_block(len(current) + 1, drop)
        if current and (
            len(current) >= _MAX_PRODUCTS_PER_MESSAGE
            or current_len + len(block) > _TELEGRAM_TEXT_LIMIT
        ):
            chunks.append(current)
            current = []
            current_len = 0
        current.append(drop)
        current_len += len(block)
    if current:
        chunks.append(current)
    return chunks


async def run_price_check(application, admin_chat_id: int) -> None:
    """Check all unique published products and send a price-drop report to admin."""
    db: Database = application.bot_data["db"]
    bot: Bot = application.bot
    destination_id = application.bot_data.get("destination_channel_id")
    min_drop = db.get_min_price_drop()

    started = time.monotonic()
    products = db.list_unique_published_products()
    total = len(products)

    if not products:
        await bot.send_message(
            chat_id=admin_chat_id,
            text="📉 <b>Price Check</b>\n\nNo published products to check.",
            parse_mode="HTML",
        )
        return

    client = get_creators_client()
    if not client or not creators_api_configured():
        await bot.send_message(
            chat_id=admin_chat_id,
            text="📉 <b>Price Check</b>\n\nCreators API is not configured.",
            parse_mode="HTML",
        )
        return

    await bot.send_message(
        chat_id=admin_chat_id,
        text=f"📉 Checking prices for <b>{total}</b> unique products…",
        parse_mode="HTML",
    )

    asin_to_product = {p["asin"].upper(): p for p in products}
    asins = list(asin_to_product.keys())
    current_prices: dict[str, tuple[str, float | None]] = {}

    for i in range(0, len(asins), 10):
        batch = asins[i : i + 10]
        try:
            items = await client.get_items(
                batch,
                PRICE_DROP_PROFILE,
                db=db,
                profile="price_drop",
            )
        except Exception:
            logger.exception("PRICE CHECK batch failed asins=%s", batch)
            for asin in batch:
                product = asin_to_product[asin]
                db.update_published_product_price_check(product["id"], None)
            continue

        for asin in batch:
            item = items.get(asin)
            product = asin_to_product[asin]
            if item and item.price != "Not found":
                numeric = parse_price_number(item.price)
                current_prices[asin] = (item.price, numeric)
                db.update_published_product_price_check(product["id"], numeric)
            else:
                db.update_published_product_price_check(product["id"], None)

    drops: list[dict[str, Any]] = []
    ignored_small_changes = 0
    for asin, product in asin_to_product.items():
        published_value = product.get("published_price_value")
        if published_value is None:
            continue
        current = current_prices.get(asin)
        if not current:
            continue
        _price_text, current_value = current
        if current_value is None:
            continue
        if current_value < published_value:
            savings = float(published_value) - current_value
            if savings < min_drop:
                ignored_small_changes += 1
                continue
            currency = product.get("published_currency") or "EGP"
            drops.append(
                {
                    "published_id": product["id"],
                    "asin": asin,
                    "title": product["title"],
                    "published_price": product.get("published_price"),
                    "published_value": float(published_value),
                    "current_price": _price_text,
                    "current_value": current_value,
                    "currency": currency,
                    "destination_channel_id": destination_id,
                    "destination_message_id": product.get("destination_message_id"),
                }
            )

    drops.sort(key=lambda d: d["published_value"] - d["current_value"], reverse=True)

    duration = time.monotonic() - started
    logger.info(
        "PRICE CHECK minimum_drop=%s products=%s checked=%s ignored_small_changes=%s drops=%s duration=%.1fs",
        min_drop,
        total,
        len(asin_to_product),
        ignored_small_changes,
        len(drops),
        duration,
    )

    if not drops:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=(
                "📉 <b>Price Drops Found</b>\n\n"
                f"Checked {total} products — no price drops detected."
            ),
            parse_mode="HTML",
        )
        return

    chunks = _chunk_drops(drops)
    for chunk_idx, chunk in enumerate(chunks):
        header = (
            "📉 <b>Price Drops Found</b>\n\n"
            f"Found {len(drops)} products with lower prices."
        )
        if len(chunks) > 1:
            header += f"\n(Page {chunk_idx + 1}/{len(chunks)})"
        header += "\n\n━━━━━━━━━━━━\n\n"

        body_parts: list[str] = []
        start_index = sum(len(c) for c in chunks[:chunk_idx])
        for i, drop in enumerate(chunk, start=start_index + 1):
            body_parts.append(_format_drop_block(i, drop))
        text = header + "\n\n━━━━━━━━━━━━\n\n".join(body_parts)
        await bot.send_message(
            chat_id=admin_chat_id,
            text=text,
            reply_markup=_build_drop_keyboard(chunk),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )


async def republish_published_product(application, published_id: int) -> str:
    """
    Re-fetch product data and publish again. Returns a short status message.
    Raises on failure.
    """
    db: Database = application.bot_data["db"]
    browser = application.bot_data.get("browser")
    destination_id = application.bot_data.get("destination_channel_id")
    if not destination_id:
        raise RuntimeError("Destination channel not configured")

    row = db.get_published_product(published_id)
    if not row:
        raise RuntimeError("Published product not found")

    asin = row["asin"]
    clean_url = build_clean_url(asin, AMAZON_DOMAIN)
    scrape_key = f"republish_{published_id}_{asin}"
    coupon_enabled = db.get_coupon_detection_enabled()

    temp_files: list[str] = []
    try:
        product = await fetch_product(
            db,
            browser,
            asin,
            clean_url,
            scrape_key,
            coupon_enabled=coupon_enabled,
        )
        display_url = resolve_display_url(product, clean_url)
        short_url = await shorten_amazon_url(display_url, db)
        if short_url:
            display_url = short_url

        temp_files.append(product["screenshot"])
        coupon = product.get("coupon") if coupon_enabled else None
        coupon_kwargs = (
            coupon_apply_kwargs_from_product(product) if coupon_enabled else {}
        )

        if product["title"] == "Not found":
            caption = build_caption(
                product["title"],
                product["price"],
                display_url,
                coupon=coupon,
                coupon_kwargs=coupon_kwargs,
            )
        else:
            caption = await build_product_caption(
                db,
                product["title"],
                product["price"],
                display_url,
                coupon=coupon,
                product=product,
            )

        upload_image = to_jpeg_for_telegram(product["screenshot"])
        if upload_image != product["screenshot"]:
            temp_files.append(upload_image)

        products = [{"title": product["title"], "url": display_url}]
        fixed_buttons = db.list_fixed_buttons(enabled_only=True)
        inline_keyboard = build_inline_keyboard(
            products,
            fixed_buttons,
            db.get_product_buttons_enabled(),
            fixed_buttons_position=db.get_fixed_buttons_position(),
            product_button_layout=db.get_product_button_layout(),
            product_button_template=db.get_product_button_template(),
            max_product_buttons=db.get_max_product_buttons(),
        )

        # Get enabled destinations
        destinations = db.get_enabled_destinations()
        if not destinations:
            return "❌ No enabled destinations configured"

        # Publish to all destinations
        result = await publish_to_destinations(
            application.bot,
            destinations,
            upload_image,
            caption,
            reply_markup=inline_keyboard if inline_keyboard.inline_keyboard else None,
            products=products,
            parse_mode="HTML",
        )
        result.log_summary()

        if result.successful == 0:
            return "❌ Failed to publish to any destination"

        price_fields = extract_published_price_fields(
            product["price"],
            product.get("list_price"),
        )
        numeric_price = price_fields["published_price_value"]

        # Update published_products for each successful destination
        for publish_result in result.results:
            if publish_result.success:
                db.update_published_product_after_republish(
                    published_id,
                    title=product["title"],
                    source_channel_id=row["source_channel_id"],
                    destination_message_id=publish_result.message_id,
                    destination_id=publish_result.destination_id,
                    published_price=price_fields["published_price"],
                    published_price_value=price_fields["published_price_value"],
                    published_list_price=price_fields["published_list_price"],
                    published_list_price_value=price_fields["published_list_price_value"],
                    published_currency=price_fields["published_currency"],
                )
                db.update_published_product_price_check(published_id, numeric_price)

                logger.info(
                    "PRICE REPUBLISH success published_id=%s asin=%s message_id=%s",
                    published_id,
                    asin,
                    publish_result.message_id,
                )

        return f"✅ Republished ASIN <code>{asin}</code> to {result.successful}/{result.total} destination(s)"
    finally:
        cleanup_files(temp_files)


async def handle_republish_drop(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    published_id = int((query.data or "").replace(CB_REPUBLISH, ""))
    db = _db(context)
    product = db.get_published_product(published_id)

    if not product:
        await query.answer("Product not found", show_alert=True)
        return

    currency = product.get("published_currency") or "EGP"
    published_price = product.get("published_price") or format_currency_amount(
        product.get("published_price_value") or 0, currency
    )
    current_price = format_currency_amount(
        product.get("last_price_check") or 0, currency
    )
    published_value = float(product.get("published_price_value") or 0)
    current_value = float(product.get("last_price_check") or 0)
    savings = published_value - current_value

    await query.edit_message_text(
        f"📢 <b>Confirm Republishing</b>\n\n"
        f"Product:\n{html_escape(short_title(product['title']))}\n\n"
        f"Published Price:\n{html_escape(published_price)}\n\n"
        f"Current Price:\n{html_escape(current_price)}\n\n"
        f"Savings:\n{html_escape(format_savings(savings, currency))}\n\n"
        f"Do you want to republish this product?",
        reply_markup=_build_republish_confirm_keyboard(published_id),
        parse_mode="HTML",
    )


async def handle_republish_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    published_id = int((query.data or "").replace(CB_REPUBLISH_CONFIRM, ""))
    await query.answer("Republishing…")

    try:
        status = await republish_published_product(context.application, published_id)
        await query.edit_message_text(status, parse_mode="HTML")
    except Exception:
        logger.exception("Republish failed published_id=%s", published_id)
        await query.edit_message_text(
            f"❌ Republish failed for id <code>{published_id}</code>",
            parse_mode="HTML",
        )


async def handle_republish_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.edit_message_text("❌ Republish cancelled.")


def build_price_monitoring_handlers() -> list:
    return [
        CallbackQueryHandler(
            handle_republish_drop,
            pattern=r"^republish_drop:\d+$",
        ),
        CallbackQueryHandler(
            handle_republish_confirm,
            pattern=r"^republish_confirm:\d+$",
        ),
        CallbackQueryHandler(
            handle_republish_cancel,
            pattern="^republish_cancel$",
        ),
    ]
