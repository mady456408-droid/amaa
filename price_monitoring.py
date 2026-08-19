"""Published price monitoring — batch Creators API checks, price history, auto scheduler, and admin reports."""

from __future__ import annotations

import asyncio
import logging
import os
import time
import re
from datetime import datetime, timedelta, timezone
from html import escape as html_escape
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from ai_caption import build_product_caption
from amazon_shortener import shorten_amazon_url
from config import ADMIN_USER_IDS, AMAZON_DOMAIN
from coupon_price import coupon_apply_kwargs_from_product, parse_price_number
from creators_api import (
    PRICE_DROP_PROFILE,
    CreatorsAPIError,
    _format_egp_price,
    creators_api_configured,
    get_creators_client,
    is_valid_asin,
)
from database import Database
from file_cleanup import cleanup_files
from inline_buttons import build_inline_keyboard
from link_resolver import build_clean_url
from product_fetcher import fetch_product, resolve_display_url
from published_price import (
    drop_index_emoji,
    extract_published_price_fields,
    format_currency_amount,
    format_detailed_price_drop_message,
    format_resale_price_drop_message,
    format_resale_restock_message,
    format_restock_message,
    format_savings,
    generate_price_chart_image,
    short_title,
)
from telegram_publisher import build_caption, publish_to_channel_with_overflow
from multi_publisher import publish_to_destinations
from upload_prep import to_jpeg_for_telegram

logger = logging.getLogger(__name__)

CB_REPUBLISH = "republish_drop:"
CB_REPUBLISH_CONFIRM = "republish_confirm:"
CB_VIEW_OLD_POST = "view_old_post:"
CB_PRICE_HISTORY_LIST = "ph_list"
CB_PRICE_HISTORY_VIEW = "ph_view:"
CB_PRICE_CHART_VIEW = "ph_chart:"

_MAX_PRODUCTS_PER_MESSAGE = 8
_TELEGRAM_TEXT_LIMIT = 4000

NEW_AMAZON_SELLER_ID = "A1ZVRGNO5AYLOV"
AMAZON_RESALE_SELLER_ID = "A2N2MP47XAP1MK"


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
            InlineKeyboardButton("📊 Price History", callback_data=f"{CB_PRICE_HISTORY_VIEW}{drop['asin']}"),
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


def extract_seller_offer(
    item: Any | None,
    seller_type: str,
) -> tuple[str, str | None, float | None, str | None, float | None, str | None]:
    """
    Extract seller offer details for seller_type ('NEW_AMAZON' or 'AMAZON_RESALE').
    Returns tuple: (status, price_text, price_value, list_price_text, list_price_value, seller_name)
    status: 'AVAILABLE', 'OUT_OF_STOCK', or 'UNKNOWN' (API failure)
    """
    if not item:
        return ("UNKNOWN", None, None, None, None, None)

    target_merchant_id = NEW_AMAZON_SELLER_ID if seller_type == "NEW_AMAZON" else AMAZON_RESALE_SELLER_ID
    listings = getattr(item, "raw_listings", []) or []
    merchant_found = False
    matching_offers = []

    for listing in listings:
        merchant_info = listing.get("merchantInfo") or {}
        m_id = merchant_info.get("id") or ""
        if m_id == target_merchant_id:
            merchant_found = True
            price_obj = listing.get("price") or {}
            price_text = _format_egp_price(price_obj.get("money"))
            if price_text != "Not found":
                val = parse_price_number(price_text)
                if val and val > 0:
                    basis = price_obj.get("savingBasis") or {}
                    basis_money = basis.get("money") if isinstance(basis, dict) else None
                    list_price_text = _format_egp_price(basis_money) if basis_money else None
                    if list_price_text == "Not found":
                        list_price_text = None
                    list_val = parse_price_number(list_price_text) if list_price_text else None

                    seller_name = (merchant_info.get("name") or "").strip() or None
                    if seller_type == "NEW_AMAZON" and not seller_name:
                        seller_name = "Amazon.eg"

                    matching_offers.append((val, price_text, list_price_text, list_val, seller_name))

    if not merchant_found:
        return ("MISSING_MERCHANT", None, None, None, None, None)

    if not matching_offers:
        return ("OUT_OF_STOCK", None, None, None, None, None)

    matching_offers.sort(key=lambda x: x[0])
    lowest_offer = matching_offers[0]
    return ("AVAILABLE", lowest_offer[1], lowest_offer[0], lowest_offer[2], lowest_offer[3], lowest_offer[4])


async def _safe_send_message(
    bot: Bot, chat_id: int | str | None, text: str, **kwargs
) -> bool:
    if not chat_id or str(chat_id).strip() in ("0", "None", ""):
        return False
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
        return True
    except Exception as exc:
        logger.warning(
            "PRICE MONITOR → ADMIN STATUS MESSAGE FAILED chat_id=%s error=%s continuing monitoring cycle",
            chat_id,
            exc,
        )
        return False


async def price_monitoring_scheduler_loop(application: Any) -> None:
    """Background task loop that automatically executes price checks on interval."""
    logger.info("PRICE MONITOR → SCHEDULER STARTED")
    try:
        while True:
            db: Database = application.bot_data["db"]
            enabled = db.get_auto_price_monitor_enabled()
            interval_min = db.get_price_monitor_interval_min()

            logger.debug("PRICE MONITOR → SCHEDULER HEARTBEAT enabled=%s interval=%s min", enabled, interval_min)

            if enabled:
                last_check_str = db.get_last_price_check_time()
                should_run = False
                if not last_check_str:
                    should_run = True
                else:
                    try:
                        last_dt = datetime.fromisoformat(last_check_str.replace("Z", "+00:00"))
                        elapsed_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60.0
                        if elapsed_min >= interval_min:
                            should_run = True
                    except Exception:
                        should_run = True

                if should_run:
                    logger.info(
                        "PRICE MONITOR → AUTOMATIC CHECK START interval_min=%s last_check=%s",
                        interval_min,
                        last_check_str,
                    )
                    admin_chat_id = (ADMIN_USER_IDS[0] if ADMIN_USER_IDS else None) or db.get_destination_channel_id()
                    try:
                        await run_price_check(application, admin_chat_id)
                    except Exception:
                        logger.exception("PRICE MONITOR → SCHEDULER RUN FAILED")
                    db.set_last_price_check_time()

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("PRICE MONITOR → SCHEDULER STOPPED")
        raise


async def run_price_check(application: Any, admin_chat_id: int | str | None = None) -> None:
    """Check all unique published products for NEW & RESALE sellers, update history & notify."""
    db: Database = application.bot_data["db"]
    bot: Bot = application.bot
    destination_id = application.bot_data.get("destination_channel_id")
    min_drop = db.get_min_price_drop()

    t_total_start = time.monotonic()

    # Stage 1: Fetch active tracked products
    t_stage1_start = time.monotonic()
    products = db.list_unique_published_products()
    t_stage1_end = time.monotonic()
    t_fetch_products = t_stage1_end - t_stage1_start

    total = len(products)
    logger.info("PRICE MONITOR → AUTOMATIC CHECK START total_products=%s min_drop=%s", total, min_drop)

    if not products:
        await _safe_send_message(
            bot,
            admin_chat_id,
            "📉 <b>Price Check</b>\n\nNo published products to check.",
            parse_mode="HTML",
        )
        return

    client = get_creators_client()
    if not client or not creators_api_configured():
        await _safe_send_message(
            bot,
            admin_chat_id,
            "📉 <b>Price Check</b>\n\nCreators API is not configured.",
            parse_mode="HTML",
        )
        return

    await _safe_send_message(
        bot,
        admin_chat_id,
        f"📉 Checking prices & updating history for <b>{total}</b> unique products…",
        parse_mode="HTML",
    )

    # Stage 2: Validate ASINs and Build ASIN batches
    t_stage2_start = time.monotonic()
    asin_to_product: dict[str, dict[str, Any]] = {}
    invalid_asins: list[tuple[str, dict[str, Any]]] = []

    for p in products:
        raw_asin = p.get("asin") or ""
        clean_asin = raw_asin.strip().upper()
        if is_valid_asin(clean_asin):
            asin_to_product[clean_asin] = p
        else:
            invalid_asins.append((raw_asin, p))
            logger.info("PRICE MONITOR → INVALID ASIN SKIPPED asin=%s reason=invalid_asin_format", raw_asin)

    valid_asins = list(asin_to_product.keys())
    t_stage2_end = time.monotonic()
    t_build_batches = t_stage2_end - t_stage2_start

    expanded_profile = [
        "offersV2.listings.price",
        "offersV2.listings.dealDetails",
        "offersV2.listings.merchantInfo",
    ]

    # Stage 3: Creators API batch requests with Bounded Concurrency (Semaphore(4)) & 429 Retry
    t_stage3_start = time.monotonic()

    sem = asyncio.Semaphore(4)
    batches = [valid_asins[i : i + 10] for i in range(0, len(valid_asins), 10)]
    total_batches = len(batches)
    batch_durations: list[float] = []

    active_tasks = 0
    active_tasks_lock = asyncio.Lock()

    async def fetch_batch(batch_index: int, batch_asins: list[str]) -> dict[str, Any]:
        nonlocal active_tasks
        async with sem:
            async with active_tasks_lock:
                active_tasks += 1
                curr_active = active_tasks

            logger.info(
                "PRICE MONITOR → BATCH START batch=%s/%s size=%s active_tasks=%s",
                batch_index + 1,
                total_batches,
                len(batch_asins),
                curr_active,
            )

            tb0 = time.monotonic()
            max_attempts = 3

            for attempt in range(1, max_attempts + 1):
                try:
                    items = await client.get_items(
                        batch_asins,
                        expanded_profile,
                        db=db,
                        profile="price_drop",
                    )
                    tb1 = time.monotonic()
                    elapsed = tb1 - tb0
                    batch_durations.append(elapsed)

                    if attempt > 1:
                        logger.info(
                            "PRICE MONITOR → BATCH SUCCESS AFTER RETRY batch=%s/%s attempt=%s duration=%.3fs",
                            batch_index + 1,
                            total_batches,
                            attempt,
                            elapsed,
                        )
                    else:
                        logger.info(
                            "PRICE MONITOR → API BATCH END batch=%s/%s elapsed=%.3fs",
                            batch_index + 1,
                            total_batches,
                            elapsed,
                        )
                    async with active_tasks_lock:
                        active_tasks -= 1
                    return items or {}
                except CreatorsAPIError as exc:
                    if exc.status_code == 429 and attempt < max_attempts:
                        retry_in = float(2 ** (attempt - 1))
                        logger.info(
                            "PRICE MONITOR → API 429 batch=%s/%s attempt=%s/3 retry_in=%.1fs",
                            batch_index + 1,
                            total_batches,
                            attempt,
                            retry_in,
                        )
                        await asyncio.sleep(retry_in)
                        continue
                    else:
                        tb1 = time.monotonic()
                        elapsed = tb1 - tb0
                        batch_durations.append(elapsed)
                        err_label = f"HTTP_{exc.status_code}" if exc.status_code else type(exc).__name__
                        logger.warning(
                            "PRICE MONITOR → BATCH FAILED batch=%s/%s error_type=%s attempts=%s",
                            batch_index + 1,
                            total_batches,
                            err_label,
                            attempt,
                        )
                        async with active_tasks_lock:
                            active_tasks -= 1
                        return {}
                except Exception as exc:
                    tb1 = time.monotonic()
                    elapsed = tb1 - tb0
                    batch_durations.append(elapsed)
                    logger.warning(
                        "PRICE MONITOR → BATCH FAILED batch=%s/%s error_type=%s attempts=%s",
                        batch_index + 1,
                        total_batches,
                        type(exc).__name__,
                        attempt,
                    )
                    async with active_tasks_lock:
                        active_tasks -= 1
                    return {}

    batch_tasks = [fetch_batch(idx, b) for idx, b in enumerate(batches)]
    batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

    fetched_items: dict[str, Any] = {}
    for res in batch_results:
        if isinstance(res, dict):
            fetched_items.update(res)

    t_stage3_end = time.monotonic()
    t_api_requests = t_stage3_end - t_stage3_start

    # Stage 5: Bulk Latest History Read
    t_stage5_start = time.monotonic()
    bulk_history = db.get_bulk_latest_price_history(valid_asins)
    t_stage5_end = time.monotonic()
    t_db_reads = t_stage5_end - t_stage5_start
    db_read_count = 1

    # Memory structures to collect DB updates for single transaction commit
    product_check_updates: list[tuple[float, int]] = []
    seller_state_updates: list[tuple[str, float | None, int, str]] = []
    history_records: list[dict[str, Any]] = []

    notifications: list[dict[str, Any]] = []
    ignored_small_changes = 0
    unchanged_count = 0
    baseline_created_count = 0
    price_drops_count = 0
    price_increases_count = 0
    restocks_count = 0
    out_of_stock_new = 0
    out_of_stock_resale = 0
    unknown_new = 0
    unknown_resale = 0
    missing_merchant_new = 0
    missing_merchant_resale = 0
    api_failures = 0

    t_parse_items = 0.0
    t_db_writes = 0.0
    t_history_proc = 0.0
    t_avail_updates = 0.0
    t_drop_calc = 0.0

    for asin, product in asin_to_product.items():
        logger.info("PRICE MONITOR → ASIN=%s", asin)

        item = fetched_items.get(asin)
        api_failed = item is None

        if api_failed:
            api_failures += 1
            unknown_new += 1
            unknown_resale += 1
            logger.info("PRICE MONITOR → SKIP REASON: ASIN=%s reason=api_batch_failed", asin)
            continue

        seller_configs = [
            ("NEW_AMAZON", NEW_AMAZON_SELLER_ID),
            ("AMAZON_RESALE", AMAZON_RESALE_SELLER_ID),
        ]

        for seller_type, seller_id in seller_configs:
            logger.info("PRICE MONITOR → ASIN=%s SELLER_TYPE=%s SELLER_ID=%s", asin, seller_type, seller_id)

            tp0 = time.monotonic()
            status, price_text, price_val, list_text, list_val, seller_name = extract_seller_offer(
                item, seller_type
            )
            tp1 = time.monotonic()
            t_parse_items += (tp1 - tp0)

            prefix = "new" if seller_type == "NEW_AMAZON" else "resale"
            prev_availability = product.get(f"{prefix}_availability") or "AVAILABLE"
            prev_last_valid = product.get(f"{prefix}_last_valid_price")

            # Bulk in-memory lookup
            latest_history = bulk_history.get((asin, seller_type))

            if prev_last_valid is None and latest_history:
                prev_last_valid = float(latest_history["final_price"])

            logger.info(
                "PRICE MONITOR → ASIN=%s SELLER=%s AVAILABILITY=%s PREV_AVAILABILITY=%s PREV_LAST_VALID=%s",
                asin,
                seller_type,
                status,
                prev_availability,
                prev_last_valid,
            )

            if status == "UNKNOWN":
                if seller_type == "NEW_AMAZON":
                    unknown_new += 1
                else:
                    unknown_resale += 1
                logger.info(
                    "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=unknown_api_response",
                    asin,
                    seller_type,
                )
                continue

            if status == "MISSING_MERCHANT":
                if seller_type == "NEW_AMAZON":
                    missing_merchant_new += 1
                else:
                    missing_merchant_resale += 1
                logger.info(
                    "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=missing_merchant_id prev_availability=%s",
                    asin,
                    seller_type,
                    prev_availability,
                )
                if prev_availability != "OUT_OF_STOCK":
                    seller_state_updates.append(("OUT_OF_STOCK", prev_last_valid, product["id"], seller_type))
                    history_records.append(
                        {
                            "asin": asin,
                            "price": None,
                            "price_value": None,
                            "final_price": prev_last_valid if prev_last_valid else 0.0,
                            "tracked_product_id": product["id"],
                            "availability": "OUT_OF_STOCK",
                            "change_type": "out_of_stock",
                            "seller_type": seller_type,
                            "seller_id": seller_id,
                        }
                    )
                continue

            if status == "OUT_OF_STOCK":
                if seller_type == "NEW_AMAZON":
                    out_of_stock_new += 1
                else:
                    out_of_stock_resale += 1
                logger.info(
                    "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=out_of_stock_offer_unavailable prev_availability=%s",
                    asin,
                    seller_type,
                    prev_availability,
                )
                if prev_availability != "OUT_OF_STOCK":
                    seller_state_updates.append(("OUT_OF_STOCK", prev_last_valid, product["id"], seller_type))
                    history_records.append(
                        {
                            "asin": asin,
                            "price": None,
                            "price_value": None,
                            "final_price": prev_last_valid if prev_last_valid else 0.0,
                            "tracked_product_id": product["id"],
                            "availability": "OUT_OF_STOCK",
                            "change_type": "out_of_stock",
                            "seller_type": seller_type,
                            "seller_id": seller_id,
                        }
                    )
                continue

            curr_final = price_val if price_val else 0.0
            product_check_updates.append((curr_final, product["id"]))

            if not latest_history:
                history_records.append(
                    {
                        "asin": asin,
                        "price": price_text,
                        "price_value": curr_final,
                        "final_price": curr_final,
                        "tracked_product_id": product["id"],
                        "list_price": list_text,
                        "list_price_value": list_val,
                        "coupon": product.get("coupon"),
                        "seller_name": seller_name,
                        "availability": "AVAILABLE",
                        "change_type": "initial",
                        "price_source": "creators_api",
                        "seller_type": seller_type,
                        "seller_id": seller_id,
                    }
                )
                seller_state_updates.append(("AVAILABLE", curr_final, product["id"], seller_type))
                baseline_created_count += 1

                if seller_type == "AMAZON_RESALE":
                    stats = db.get_price_history_stats(asin, seller_type=seller_type, current_override_price=curr_final)
                    currency = product.get("published_currency") or "EGP"
                    msg_text = format_resale_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=0.0,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={AMAZON_RESALE_SELLER_ID}",
                    )
                    notifications.append(
                        {
                            "published_id": product["id"],
                            "asin": asin,
                            "message_text": msg_text,
                        }
                    )
                    logger.info(
                        "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=None previous_availability=%s alert_decision=ALERT_QUEUED alert_reason=INITIAL_RESALE_OFFER_AVAILABLE",
                        asin,
                        AMAZON_RESALE_SELLER_ID,
                        curr_final,
                        prev_availability,
                    )
                else:
                    logger.info(
                        "PRICE MONITOR → INITIAL BASELINE RECORD SAVED asin=%s seller_type=%s price=%.2f (no alert)",
                        asin,
                        seller_type,
                        curr_final,
                    )
                continue

            is_restock = prev_availability == "OUT_OF_STOCK"
            effective_prev = prev_last_valid if (prev_last_valid and prev_last_valid > 0) else float(latest_history["final_price"])

            price_diff = curr_final - effective_prev
            has_price_change = abs(price_diff) >= 0.01

            if is_restock:
                restocks_count += 1
                seller_state_updates.append(("AVAILABLE", curr_final, product["id"], seller_type))
                history_records.append(
                    {
                        "asin": asin,
                        "price": price_text,
                        "price_value": curr_final,
                        "final_price": curr_final,
                        "tracked_product_id": product["id"],
                        "list_price": list_text,
                        "list_price_value": list_val,
                        "coupon": product.get("coupon"),
                        "seller_name": seller_name,
                        "availability": "AVAILABLE",
                        "price_change_amount": price_diff,
                        "change_type": "restock",
                        "seller_type": seller_type,
                        "seller_id": seller_id,
                    }
                )

                stats = db.get_price_history_stats(asin, seller_type=seller_type, current_override_price=curr_final)
                currency = product.get("published_currency") or "EGP"

                tc0 = time.monotonic()
                if seller_type == "NEW_AMAZON":
                    msg_text = format_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={NEW_AMAZON_SELLER_ID}",
                    )
                    logger.info("PRICE MONITOR → RESTOCK NOTIFICATION QUEUED asin=%s seller_type=%s", asin, seller_type)
                else:
                    msg_text = format_resale_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={AMAZON_RESALE_SELLER_ID}",
                    )
                    logger.info(
                        "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=%.2f previous_availability=%s alert_decision=ALERT_QUEUED alert_reason=RESALE_RESTOCK",
                        asin,
                        AMAZON_RESALE_SELLER_ID,
                        curr_final,
                        effective_prev,
                        prev_availability,
                    )
                tc1 = time.monotonic()
                t_drop_calc += (tc1 - tc0)

                notifications.append(
                    {
                        "published_id": product["id"],
                        "asin": asin,
                        "message_text": msg_text,
                    }
                )
                continue

            if not has_price_change:
                unchanged_count += 1
                if seller_type == "AMAZON_RESALE":
                    logger.info(
                        "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=%.2f previous_availability=%s alert_decision=SKIP_ALERT alert_reason=PRICE_UNCHANGED",
                        asin,
                        AMAZON_RESALE_SELLER_ID,
                        curr_final,
                        effective_prev,
                        prev_availability,
                    )
                else:
                    logger.info(
                        "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=price_unchanged price=%.2f",
                        asin,
                        seller_type,
                        curr_final,
                    )
                seller_state_updates.append(("AVAILABLE", curr_final, product["id"], seller_type))
                continue

            change_amount = price_diff
            change_percent = (change_amount / effective_prev * 100.0) if effective_prev > 0 else 0.0
            change_type = "price_drop" if curr_final < effective_prev else "price_increase"

            if change_type == "price_increase":
                price_increases_count += 1
                if seller_type == "AMAZON_RESALE":
                    logger.info(
                        "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=%.2f previous_availability=%s alert_decision=SKIP_ALERT alert_reason=PRICE_INCREASE",
                        asin,
                        AMAZON_RESALE_SELLER_ID,
                        curr_final,
                        effective_prev,
                        prev_availability,
                    )
                else:
                    logger.info(
                        "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=price_increase previous=%.2f current=%.2f",
                        asin,
                        seller_type,
                        effective_prev,
                        curr_final,
                    )

            history_records.append(
                {
                    "asin": asin,
                    "price": price_text,
                    "price_value": curr_final,
                    "final_price": curr_final,
                    "tracked_product_id": product["id"],
                    "list_price": list_text,
                    "list_price_value": list_val,
                    "coupon": product.get("coupon"),
                    "seller_name": seller_name,
                    "availability": "AVAILABLE",
                    "price_change_amount": change_amount,
                    "price_change_percent": change_percent,
                    "change_type": change_type,
                    "seller_type": seller_type,
                    "seller_id": seller_id,
                }
            )
            seller_state_updates.append(("AVAILABLE", curr_final, product["id"], seller_type))

            logger.info(
                "PRICE MONITOR → HISTORY RECORD SAVED asin=%s seller_type=%s previous=%.2f current=%.2f change=%.2f percent=%.1f%% type=%s",
                asin,
                seller_type,
                effective_prev,
                curr_final,
                change_amount,
                change_percent,
                change_type,
            )

            if curr_final < effective_prev:
                price_drops_count += 1
                savings = effective_prev - curr_final
                if savings < min_drop:
                    ignored_small_changes += 1
                    if seller_type == "AMAZON_RESALE":
                        logger.info(
                            "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=%.2f previous_availability=%s alert_decision=SKIP_ALERT alert_reason=BELOW_MIN_DROP",
                            asin,
                            AMAZON_RESALE_SELLER_ID,
                            curr_final,
                            effective_prev,
                            prev_availability,
                        )
                    else:
                        logger.info(
                            "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=below_min_drop savings=%.2f min_drop=%s",
                            asin,
                            seller_type,
                            savings,
                            min_drop,
                        )
                    continue

                stats = db.get_price_history_stats(asin, seller_type=seller_type, current_override_price=curr_final)
                currency = product.get("published_currency") or "EGP"

                tc0 = time.monotonic()
                if seller_type == "NEW_AMAZON":
                    msg_text = format_detailed_price_drop_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        currency=currency,
                        stats=stats,
                        coupon=product.get("coupon"),
                        seller=seller_name,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={NEW_AMAZON_SELLER_ID}",
                    )
                else:
                    msg_text = format_resale_price_drop_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={AMAZON_RESALE_SELLER_ID}",
                    )
                    logger.info(
                        "RESALE OFFER DETECTED asin=%s merchant_id=%s current_price=%.2f previous_price=%.2f previous_availability=%s alert_decision=ALERT_QUEUED alert_reason=PRICE_DROP",
                        asin,
                        AMAZON_RESALE_SELLER_ID,
                        curr_final,
                        effective_prev,
                        prev_availability,
                    )
                tc1 = time.monotonic()
                t_drop_calc += (tc1 - tc0)

                notifications.append(
                    {
                        "published_id": product["id"],
                        "asin": asin,
                        "message_text": msg_text,
                    }
                )

    # Stage 6: Atomic Bulk Database Updates
    t_stage6_start = time.monotonic()
    if product_check_updates or seller_state_updates or history_records:
        db.execute_bulk_monitoring_db_updates(
            product_check_updates, seller_state_updates, history_records
        )
    t_stage6_end = time.monotonic()
    t_db_writes = t_stage6_end - t_stage6_start
    db_write_count = len(product_check_updates) + len(seller_state_updates) + len(history_records)

    total_seller_checks = total * 2
    invalid_asins_new = len(invalid_asins)
    invalid_asins_resale = len(invalid_asins)

    sum_seller_outcomes = (
        baseline_created_count
        + unchanged_count
        + price_drops_count
        + price_increases_count
        + restocks_count
        + out_of_stock_new
        + out_of_stock_resale
        + missing_merchant_new
        + missing_merchant_resale
        + unknown_new
        + unknown_resale
        + invalid_asins_new
        + invalid_asins_resale
    )

    if sum_seller_outcomes != total_seller_checks:
        logger.error(
            "PRICE MONITOR → RECONCILIATION ERROR: sum_outcomes=%s != expected=%s",
            sum_seller_outcomes,
            total_seller_checks,
        )
        assert sum_seller_outcomes == total_seller_checks, (
            f"Summary counter reconciliation failed: sum({sum_seller_outcomes}) != expected({total_seller_checks})"
        )

    t_stage10_start = time.monotonic()
    summary_msg = (
        "📉 <b>Price Check Complete</b>\n\n"
        f"Checked <b>{total}</b> unique products (<b>{total_seller_checks}</b> seller condition checks):\n"
        f"• Initial Baselines Created: <b>{baseline_created_count}</b>\n"
        f"• Unchanged Prices: <b>{unchanged_count}</b>\n"
        f"• Price Drops Detected: <b>{price_drops_count}</b> ({ignored_small_changes} below min drop)\n"
        f"• Price Increases: <b>{price_increases_count}</b>\n"
        f"• Restocks: <b>{restocks_count}</b>\n"
        f"• Out of Stock (NEW Amazon): <b>{out_of_stock_new}</b>\n"
        f"• Out of Stock (Amazon Resale): <b>{out_of_stock_resale}</b>\n"
        f"• Missing Merchant (NEW Amazon): <b>{missing_merchant_new}</b>\n"
        f"• Missing Merchant (Amazon Resale): <b>{missing_merchant_resale}</b>\n"
    )
    if invalid_asins_new > 0:
        summary_msg += f"• Invalid ASINs Skipped: <b>{len(invalid_asins)}</b> ({invalid_asins_new + invalid_asins_resale} seller checks)\n"
    if unknown_new > 0 or unknown_resale > 0 or api_failures > 0:
        summary_msg += f"• API Failures / Unknown: <b>{unknown_new + unknown_resale}</b> (ASINs failed: {api_failures})\n"

    if not notifications:
        await _safe_send_message(
            bot,
            admin_chat_id,
            summary_msg + "\nNo alerts queued.",
            parse_mode="HTML",
        )
    else:
        for notif in notifications:
            reply_markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📢 Republish",
                            callback_data=f"{CB_REPUBLISH}{notif['published_id']}",
                        ),
                        InlineKeyboardButton(
                            "📊 Price History",
                            callback_data=f"{CB_PRICE_HISTORY_VIEW}{notif['asin']}",
                        ),
                    ]
                ]
            )
            sent = await _safe_send_message(
                bot,
                admin_chat_id,
                text=notif["message_text"],
                reply_markup=reply_markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            if sent:
                logger.info("PRICE MONITOR → NOTIFICATION SENT asin=%s", notif["asin"])

        await _safe_send_message(
            bot,
            admin_chat_id,
            summary_msg + f"\n🔔 <b>{len(notifications)} alert(s) sent.</b>",
            parse_mode="HTML",
        )

    t_stage10_end = time.monotonic()
    t_telegram_notifs = t_stage10_end - t_stage10_start

    t_total_end = time.monotonic()
    t_total = t_total_end - t_total_start

    total_batches = (total + 9) // 10 if total > 0 else 0
    avg_api = (sum(batch_durations) / len(batch_durations)) if batch_durations else 0.0
    slowest_api = max(batch_durations) if batch_durations else 0.0

    logger.info(
        "PRICE MONITOR → PROFILING SUMMARY:\n"
        "• Stage 1 — Fetch active products: %.3fs\n"
        "• Stage 2 — Build ASIN batches: %.3fs\n"
        "• Stage 3 — Creators API batch requests: %.3fs\n"
        "  - Total ASINs: %s\n"
        "  - Total Batches: %s\n"
        "  - Batch Size: 10\n"
        "  - Total API Requests: %s\n"
        "  - Average Request Duration: %.3fs\n"
        "  - Slowest Request Duration: %.3fs\n"
        "• Stage 4 — Product/offer parsing: %.3fs\n"
        "• Stage 5 — Database reads: %.3fs (%s queries)\n"
        "• Stage 6 — Database writes: %.3fs (%s writes)\n"
        "• Stage 7 — Price history processing: %.3fs\n"
        "• Stage 8 — Availability state updates: %.3fs\n"
        "• Stage 9 — Drop/restock calculations: %.3fs\n"
        "• Stage 10 — Telegram notifications: %.3fs\n"
        "• Stage 11 — Total cycle time: %.3fs",
        t_fetch_products,
        t_build_batches,
        t_api_requests,
        total,
        total_batches,
        len(batch_durations),
        avg_api,
        slowest_api,
        t_parse_items,
        t_db_reads,
        db_read_count,
        t_db_writes,
        db_write_count,
        t_history_proc,
        t_avail_updates,
        t_drop_calc,
        t_telegram_notifs,
        t_total,
    )


async def republish_published_product(application: Any, published_id: int) -> str:
    """Re-fetch product data and publish again."""
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

        destinations = db.get_enabled_destinations()
        if not destinations:
            return "❌ No enabled destinations configured"

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


async def handle_price_history_list(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Show list of tracked products to inspect price history."""
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    db = _db(context)
    products = db.list_unique_published_products()

    if not products:
        await query.edit_message_text(
            "📊 <b>Price History</b>\n\nNo tracked products found.",
            parse_mode="HTML",
        )
        return

    rows: list[list[InlineKeyboardButton]] = []
    for p in products[:15]:
        title_short = short_title(p["title"], 30)
        rows.append(
            [
                InlineKeyboardButton(
                    f"📦 {title_short}",
                    callback_data=f"{CB_PRICE_HISTORY_VIEW}{p['asin']}",
                )
            ]
        )

    rows.append([InlineKeyboardButton("🔙 Back to Dashboard", callback_data="adm:price_monitor")])

    await query.edit_message_text(
        "📊 <b>Select Product for Price History:</b>\n\n"
        "Choose a product below to inspect its historical price record and chart.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode="HTML",
    )


async def handle_price_history_view(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """View detailed price history text and stats for an ASIN."""
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    asin = (query.data or "").replace(CB_PRICE_HISTORY_VIEW, "").strip().upper()
    db = _db(context)
    stats = db.get_price_history_stats(asin, seller_type="NEW_AMAZON")

    if not stats.get("has_data"):
        await query.edit_message_text(
            f"📊 <b>Price History — {asin}</b>\n\nNo price history recorded yet.",
            parse_mode="HTML",
        )
        return

    records = db.get_price_history_records(asin, seller_type="NEW_AMAZON", limit=10)

    lines = [
        f"📊 <b>Price History — {asin}</b>\n",
        f"💰 <b>Current Price:</b> {format_currency_amount(stats['current_price'])}",
        f"🏆 <b>Lowest Price:</b> {format_currency_amount(stats['lowest_price'])}",
        f"📈 <b>Highest Price:</b> {format_currency_amount(stats['highest_price'])}",
        f"📊 <b>Average Price:</b> {format_currency_amount(stats['average_price'])}",
        f"🔄 <b>Total Changes:</b> {stats['total_changes']}\n",
        "<b>Recent Timeline (NEW Amazon):</b>",
    ]

    for r in records:
        raw_dt = r.get("recorded_at") or ""
        date_str = raw_dt[:10] if raw_dt else "N/A"
        price_str = format_currency_amount(r["final_price"])
        chg_type = r.get("change_type") or "record"
        lines.append(f"• {date_str} — {price_str} ({chg_type})")

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📈 View Price Chart", callback_data=f"{CB_PRICE_CHART_VIEW}{asin}"),
                InlineKeyboardButton("🔙 Back to List", callback_data=CB_PRICE_HISTORY_LIST),
            ]
        ]
    )

    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def handle_price_chart_view(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Generate and send matplotlib price chart image for an ASIN."""
    query = update.callback_query
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    asin = (query.data or "").replace(CB_PRICE_CHART_VIEW, "").strip().upper()
    await query.answer("Generating price chart…")

    db = _db(context)
    records = db.get_price_history_records(asin, seller_type="NEW_AMAZON", limit=100)

    if not records or len(records) < 2:
        await query.message.reply_text(
            f"ℹ️ Not enough history data to generate chart for ASIN <code>{asin}</code> (at least 2 price checks required).",
            parse_mode="HTML",
        )
        return

    chart_path = generate_price_chart_image(asin, asin, records)
    if not chart_path or not os.path.exists(chart_path):
        await query.message.reply_text("❌ Failed to generate price chart.")
        return

    try:
        with open(chart_path, "rb") as photo:
            await query.message.reply_photo(
                photo=photo,
                caption=f"📈 <b>Price History Chart — {asin}</b>",
                parse_mode="HTML",
            )
    finally:
        cleanup_files([chart_path])


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
        CallbackQueryHandler(
            handle_price_history_list,
            pattern=r"^ph_list$",
        ),
        CallbackQueryHandler(
            handle_price_history_view,
            pattern=r"^ph_view:.+$",
        ),
        CallbackQueryHandler(
            handle_price_chart_view,
            pattern=r"^ph_chart:.+$",
        ),
    ]
