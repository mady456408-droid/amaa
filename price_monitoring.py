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
from ai_rewriter import rewrite_caption
from amazon_shortener import shorten_amazon_url
from config import ADMIN_USER_IDS, AMAZON_DOMAIN
from coupon_price import coupon_apply_kwargs_from_product, parse_price_number
from creators_api import (
    AMAZON_RESALE_SELLER_ID,
    NEW_AMAZON_SELLER_ID,
    PRICE_DROP_PROFILE,
    CreatorsAPIError,
    _format_egp_price,
    creators_api_configured,
    extract_seller_offer,
    get_creators_client,
    is_valid_asin,
)
from database import Database, compute_reference_price
from file_cleanup import cleanup_files
from inline_buttons import build_inline_keyboard
from link_resolver import build_clean_url, resolve_asin_from_input
from product_fetcher import fetch_product, resolve_display_url
from published_price import (
    drop_index_emoji,
    extract_published_price_fields,
    format_currency_amount,
    format_detailed_price_drop_message,
    format_resale_price_drop_message,
    format_resale_restock_message,
    format_resale_smart_restock_message,
    format_restock_message,
    format_savings,
    format_smart_restock_message,
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
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logger.info("PRICE MONITOR → SCHEDULER STOPPED")
        raise


async def evaluate_product_price_check(
    db: Database,
    product: dict[str, Any],
    item: Any | None,
    bulk_history: dict[tuple[str, str], dict[str, Any]],
    min_drop: float = 1.0,
) -> dict[str, Any]:
    """
    Core per-product price evaluation logic. Shared identically between
    automatic monitoring and single-product checks.
    """
    asin = product["asin"].upper()
    api_failed = item is None

    product_check_updates: list[tuple[float, int]] = []
    seller_state_updates: list[tuple[str, float | None, int, str]] = []
    history_records: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    seller_evaluations: dict[str, dict[str, Any]] = {}

    counts = {
        "baseline_created": 0,
        "unchanged": 0,
        "price_drops": 0,
        "price_increases": 0,
        "restocks": 0,
        "out_of_stock_new": 0,
        "out_of_stock_resale": 0,
        "missing_merchant_new": 0,
        "missing_merchant_resale": 0,
        "unknown_new": 0,
        "unknown_resale": 0,
        "ignored_small_changes": 0,
        "api_failures": 0,
    }

    if api_failed:
        counts["api_failures"] += 1
        counts["unknown_new"] += 1
        counts["unknown_resale"] += 1
        logger.info("PRICE MONITOR → SKIP REASON: ASIN=%s reason=api_batch_failed", asin)
        return {
            "asin": asin,
            "api_failed": True,
            "product_check_updates": [],
            "seller_state_updates": [],
            "history_records": [],
            "notifications": [],
            "seller_evaluations": {},
            "counts": counts,
        }

    seller_configs = [
        ("NEW_AMAZON", NEW_AMAZON_SELLER_ID),
        ("AMAZON_RESALE", AMAZON_RESALE_SELLER_ID),
    ]

    for seller_type, seller_id in seller_configs:
        status, price_text, price_val, list_text, list_val, seller_name = extract_seller_offer(
            item, seller_type
        )

        prefix = "new" if seller_type == "NEW_AMAZON" else "resale"
        prev_availability = product.get(f"{prefix}_availability") or "AVAILABLE"
        prev_last_valid = product.get(f"{prefix}_last_valid_price")

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

        seller_eval = {
            "status": status,
            "price_text": price_text,
            "price_val": price_val,
            "curr_final": price_val if price_val else 0.0,
            "prev_last_valid": prev_last_valid,
            "prev_availability": prev_availability,
            "seller_name": seller_name,
            "change_type": None,
            "is_baseline": False,
            "is_restock": False,
        }
        seller_evaluations[seller_type] = seller_eval

        if status == "UNKNOWN":
            if seller_type == "NEW_AMAZON":
                counts["unknown_new"] += 1
            else:
                counts["unknown_resale"] += 1
            logger.info(
                "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=unknown_api_response",
                asin,
                seller_type,
            )
            continue

        if status == "MISSING_MERCHANT":
            if seller_type == "NEW_AMAZON":
                counts["missing_merchant_new"] += 1
            else:
                counts["missing_merchant_resale"] += 1
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
                counts["out_of_stock_new"] += 1
            else:
                counts["out_of_stock_resale"] += 1
            logger.info(
                "PRICE MONITOR → SKIP REASON: ASIN=%s seller_type=%s reason=out_of_stock_offer_unavailable prev_availability=%s",
                asin,
                seller_type,
                prev_availability,
            )
            if prev_availability != "OUT_OF_STOCK":
                stored_ref = product.get("new_reference_price") if seller_type == "NEW_AMAZON" else product.get("resale_reference_price")
                seller_state_updates.append(("OUT_OF_STOCK", prev_last_valid, stored_ref, product["id"], seller_type))
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

        # Calculate robust reference price for this (asin, seller_type)
        ph_recs = db.get_price_history_records(asin, seller_type=seller_type, limit=100)
        curr_rec = {"final_price": curr_final, "availability": "AVAILABLE"}
        calc_ref = compute_reference_price(ph_recs + [curr_rec])
        stored_ref = product.get("new_reference_price") if seller_type == "NEW_AMAZON" else product.get("resale_reference_price")

        if stored_ref is not None and stored_ref > 0:
            if calc_ref is not None and calc_ref > 0:
                ref_price = max(stored_ref, calc_ref)
            else:
                ref_price = stored_ref
        else:
            ref_price = calc_ref if calc_ref is not None else curr_final

        ref_discount_pct = ((ref_price - curr_final) / ref_price * 100.0) if (ref_price and ref_price > 0) else 0.0

        if not latest_history:
            seller_eval["is_baseline"] = True
            seller_eval["change_type"] = "initial"
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
            seller_state_updates.append(("AVAILABLE", curr_final, ref_price, product["id"], seller_type))
            counts["baseline_created"] += 1

            logger.info(
                "REFERENCE PRICE: asin=%s seller_type=%s reference_price=%.2f current_price=%.2f last_valid_price=None reference_discount_percent=%.2f",
                asin,
                seller_type,
                ref_price,
                curr_final,
                ref_discount_pct,
            )

            if seller_type == "AMAZON_RESALE":
                stats = db.get_price_history_stats(asin, seller_type=seller_type, current_override_price=curr_final)
                currency = product.get("published_currency") or "EGP"
                msg_text = format_resale_restock_message(
                    title=product["title"],
                    current_price=curr_final,
                    previous_price=0.0,
                    reference_price=ref_price,
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

        logger.info(
            "REFERENCE PRICE: asin=%s seller_type=%s reference_price=%.2f current_price=%.2f last_valid_price=%.2f reference_discount_percent=%.2f",
            asin,
            seller_type,
            ref_price,
            curr_final,
            effective_prev,
            ref_discount_pct,
        )

        if is_restock:
            seller_eval["is_restock"] = True
            seller_eval["change_type"] = "restock"
            counts["restocks"] += 1
            seller_state_updates.append(("AVAILABLE", curr_final, ref_price, product["id"], seller_type))
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
            deal_threshold_pct = db.get_smart_restock_deal_pct()

            # Priority 1: Price Drop + Restock (curr_final < effective_prev)
            if curr_final < effective_prev:
                reason = "price_drop_and_restock"
                if seller_type == "NEW_AMAZON":
                    msg_text = format_detailed_price_drop_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={NEW_AMAZON_SELLER_ID}",
                        coupon=product.get("coupon"),
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
            # Priority 2: Smart Restock Deal (ref_discount_pct >= deal_threshold_pct)
            elif ref_discount_pct >= deal_threshold_pct and ref_price and ref_price > curr_final:
                reason = "smart_restock_deal"
                if seller_type == "NEW_AMAZON":
                    msg_text = format_smart_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        reference_price=ref_price,
                        previous_price=effective_prev,
                        currency=currency,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={NEW_AMAZON_SELLER_ID}",
                    )
                else:
                    msg_text = format_resale_smart_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        reference_price=ref_price,
                        previous_price=effective_prev,
                        currency=currency,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={AMAZON_RESALE_SELLER_ID}",
                    )
            # Priority 3: Normal Restock
            else:
                reason = "normal_restock"
                if seller_type == "NEW_AMAZON":
                    msg_text = format_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        reference_price=ref_price,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={NEW_AMAZON_SELLER_ID}",
                    )
                else:
                    msg_text = format_resale_restock_message(
                        title=product["title"],
                        current_price=curr_final,
                        previous_price=effective_prev,
                        reference_price=ref_price,
                        currency=currency,
                        stats=stats,
                        product_url=f"https://{AMAZON_DOMAIN}/dp/{asin}?m={AMAZON_RESALE_SELLER_ID}",
                    )

            logger.info(
                "SMART RESTOCK DECISION: asin=%s seller_type=%s current=%.2f reference=%.2f last_valid=%.2f qualifies=%s reason=%s",
                asin,
                seller_type,
                curr_final,
                ref_price or 0.0,
                effective_prev,
                reason in ("price_drop_and_restock", "smart_restock_deal"),
                reason,
            )

            notifications.append(
                {
                    "published_id": product["id"],
                    "asin": asin,
                    "message_text": msg_text,
                }
            )
            continue

        if not has_price_change:
            counts["unchanged"] += 1
            seller_eval["change_type"] = "unchanged"
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
            seller_state_updates.append(("AVAILABLE", curr_final, ref_price, product["id"], seller_type))
            continue

        change_amount = price_diff
        change_percent = (change_amount / effective_prev * 100.0) if effective_prev > 0 else 0.0
        change_type = "price_drop" if curr_final < effective_prev else "price_increase"
        seller_eval["change_type"] = change_type

        if change_type == "price_increase":
            counts["price_increases"] += 1
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
        seller_state_updates.append(("AVAILABLE", curr_final, ref_price, product["id"], seller_type))

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
            counts["price_drops"] += 1
            savings = effective_prev - curr_final
            if savings < min_drop:
                counts["ignored_small_changes"] += 1
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

            notifications.append(
                {
                    "published_id": product["id"],
                    "asin": asin,
                    "message_text": msg_text,
                }
            )

    return {
        "asin": asin,
        "api_failed": False,
        "product_check_updates": product_check_updates,
        "seller_state_updates": seller_state_updates,
        "history_records": history_records,
        "notifications": notifications,
        "seller_evaluations": seller_evaluations,
        "counts": counts,
    }


def _format_single_product_result_card(
    asin: str,
    title: str,
    existing_product: bool,
    new_eval: dict[str, Any],
    resale_eval: dict[str, Any],
    new_stats: dict[str, Any] | None,
    resale_stats: dict[str, Any] | None,
    api_failed: bool = False,
) -> str:
    lines = [
        "🔎 <b>Price Check Result</b>\n",
        f"📦 <b>{html_escape(title)}</b>",
        f"ASIN: <code>{asin}</code>\n",
    ]

    if api_failed:
        lines.append("⚠️ <b>Creators API Status: UNKNOWN</b>\n<i>State preserved without mutating database.</i>\n")

    # NEW Amazon Section
    new_status = new_eval.get("status", "UNKNOWN")
    new_price = new_eval.get("curr_final", 0.0)
    new_seller = new_eval.get("seller_name") or "Amazon.eg"
    if new_status == "AVAILABLE" and new_price > 0:
        lines.append("🆕 <b>Amazon:</b>")
        lines.append(f"💰 <b>{new_price:,.2f} EGP</b>")
        lines.append(f"🏪 {html_escape(new_seller)}")
        lines.append("🟢 Available\n")
    elif new_status == "OUT_OF_STOCK":
        lines.append("🆕 <b>Amazon:</b> 🔴 Out of Stock\n")
    elif new_status == "MISSING_MERCHANT":
        lines.append("🆕 <b>Amazon:</b> ⚠️ Merchant Unavailable\n")
    else:
        lines.append("🆕 <b>Amazon:</b> ⚠️ Status Unknown\n")

    # Resale Section
    resale_status = resale_eval.get("status", "UNKNOWN")
    resale_price = resale_eval.get("curr_final", 0.0)
    if resale_status == "AVAILABLE" and resale_price > 0:
        lines.append("♻️ <b>Amazon Resale:</b>")
        lines.append(f"💰 <b>{resale_price:,.2f} EGP</b>")
        lines.append("🏪 Amazon Resale")
        lines.append("🟢 Available\n")
    elif resale_status == "OUT_OF_STOCK":
        lines.append("♻️ <b>Amazon Resale:</b> 🔴 Out of Stock\n")
    elif resale_status == "MISSING_MERCHANT":
        lines.append("♻️ <b>Amazon Resale:</b> ⚠️ Merchant Unavailable\n")
    else:
        lines.append("♻️ <b>Amazon Resale:</b> ⚠️ Status Unknown\n")

    # History Section
    hist_lines = []
    if new_stats and new_stats.get("lowest"):
        hist_lines.append(
            f"• NEW Lowest: <b>{new_stats['lowest']:,.2f} EGP</b> | Avg: <b>{new_stats.get('average', 0.0):,.2f} EGP</b>"
        )
    if resale_stats and resale_stats.get("lowest"):
        hist_lines.append(
            f"• Resale Lowest: <b>{resale_stats['lowest']:,.2f} EGP</b> | Avg: <b>{resale_stats.get('average', 0.0):,.2f} EGP</b>"
        )

    if hist_lines:
        lines.append("📊 <b>History:</b>")
        lines.extend(hist_lines)
        lines.append("")

    if not existing_product:
        lines.append("✅ <b>Added to Price Monitoring</b>")
    else:
        lines.append("✅ <b>Product already tracked</b>")
        lines.append("🔄 Existing history preserved")
        lines.append("✅ Monitoring state updated")

    return "\n".join(lines)


async def run_single_product_price_check(db: Database, input_text: str) -> dict[str, Any]:
    """
    Check a single product given an ASIN or Amazon URL.
    Reuses the SAME core evaluation engine as automatic price monitoring.
    """
    asin = await resolve_asin_from_input(input_text)
    if not asin or not is_valid_asin(asin):
        return {
            "success": False,
            "error": "invalid_asin",
            "message": "❌ Invalid Amazon ASIN",
        }

    row = db.get_published_product_by_asin(asin)
    existing_product = row is not None

    logger.info(
        "PRICE MONITOR → SINGLE CHECK START input=%s asin=%s existing_product=%s",
        input_text,
        asin,
        existing_product,
    )

    client = get_creators_client()
    if not client or not creators_api_configured():
        return {
            "success": False,
            "error": "api_not_configured",
            "message": "❌ Creators API is not configured.",
        }

    expanded_profile = [
        "offersV2.listings.price",
        "offersV2.listings.dealDetails",
        "offersV2.listings.merchantInfo",
    ]

    logger.info("PRICE MONITOR → SINGLE CHECK FETCH START asin=%s", asin)
    fetched_items = await client.get_items([asin], expanded_profile, db=db, profile="price_drop")
    item = fetched_items.get(asin)

    if not existing_product:
        logger.info("PRICE MONITOR → SINGLE CHECK NEW PRODUCT asin=%s", asin)
        title = item.title if (item and item.title and item.title != "Not found") else f"Amazon Product ({asin})"
        new_price_val = None
        new_price_txt = None
        if item and hasattr(item, "offers") and isinstance(item.offers, dict) and "NEW_AMAZON" in item.offers:
            new_price_val = item.offers["NEW_AMAZON"].get("price_value")
            new_price_txt = item.offers["NEW_AMAZON"].get("price_text")

        db.add_published_product(
            asin=asin,
            title=title,
            source_channel_id=0,
            destination_message_id=0,
            published_price=new_price_txt,
            published_price_value=new_price_val,
            published_currency="EGP",
        )
        product = db.get_published_product_by_asin(asin)
    else:
        logger.info("PRICE MONITOR → SINGLE CHECK DATABASE HIT asin=%s", asin)
        product = row

    if not product:
        return {
            "success": False,
            "error": "db_error",
            "message": "❌ Failed to load product from database.",
        }

    bulk_history = db.get_bulk_latest_price_history([asin])
    eval_res = await evaluate_product_price_check(db, product, item, bulk_history, min_drop=1.0)

    # Commit DB updates
    if eval_res["product_check_updates"] or eval_res["seller_state_updates"] or eval_res["history_records"]:
        db.execute_bulk_monitoring_db_updates(
            eval_res["product_check_updates"],
            eval_res["seller_state_updates"],
            eval_res["history_records"],
        )

    # Log required diagnostic steps
    seller_evals = eval_res.get("seller_evaluations", {})
    for stype, sid in [("NEW_AMAZON", NEW_AMAZON_SELLER_ID), ("AMAZON_RESALE", AMAZON_RESALE_SELLER_ID)]:
        seval = seller_evals.get(stype, {})
        status = seval.get("status", "UNKNOWN")
        curr_price = seval.get("curr_final")
        logger.info(
            "PRICE MONITOR → SINGLE CHECK %s merchant_id=%s price=%s availability=%s",
            stype,
            sid,
            curr_price,
            status,
        )
        if seval.get("is_baseline"):
            logger.info("PRICE MONITOR → SINGLE CHECK BASELINE CREATED seller_type=%s", stype)

    logger.info("PRICE MONITOR → SINGLE CHECK COMPLETE asin=%s added_to_monitoring=True", asin)

    new_eval = seller_evals.get("NEW_AMAZON", {})
    resale_eval = seller_evals.get("AMAZON_RESALE", {})

    new_stats = db.get_price_history_stats(asin, seller_type="NEW_AMAZON")
    resale_stats = db.get_price_history_stats(asin, seller_type="AMAZON_RESALE")

    card_text = _format_single_product_result_card(
        asin=asin,
        title=product["title"],
        existing_product=existing_product,
        new_eval=new_eval,
        resale_eval=resale_eval,
        new_stats=new_stats,
        resale_stats=resale_stats,
        api_failed=eval_res["api_failed"],
    )

    return {
        "success": True,
        "asin": asin,
        "existing_product": existing_product,
        "message": card_text,
        "eval_res": eval_res,
    }


async def run_price_check(application: Any, admin_chat_id: int | str | None = None) -> None:
    """Check all unique published products for NEW & RESALE sellers, update history & notify."""
    db: Database = application.bot_data["db"]
    bot: Bot = application.bot
    destination_id = application.bot_data.get("destination_channel_id")
    min_drop = db.get_min_price_drop()

    t_total_start = time.monotonic()

    logger.info("PRICE MONITOR → EVENT LOOP TEST START")

    # Stage 1: Fetch active tracked products
    t_stage1_start = time.monotonic()
    products = db.list_unique_published_products()
    t_stage1_end = time.monotonic()
    t_fetch_products = t_stage1_end - t_stage1_start

    total = len(products)
    logger.info("PRICE MONITOR → CYCLE START total_products=%s min_drop=%s", total, min_drop)
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
        eval_res = await evaluate_product_price_check(db, product, item, bulk_history, min_drop)

        product_check_updates.extend(eval_res["product_check_updates"])
        seller_state_updates.extend(eval_res["seller_state_updates"])
        history_records.extend(eval_res["history_records"])
        notifications.extend(eval_res["notifications"])

        c = eval_res["counts"]
        baseline_created_count += c["baseline_created"]
        unchanged_count += c["unchanged"]
        price_drops_count += c["price_drops"]
        price_increases_count += c["price_increases"]
        restocks_count += c["restocks"]
        out_of_stock_new += c["out_of_stock_new"]
        out_of_stock_resale += c["out_of_stock_resale"]
        missing_merchant_new += c["missing_merchant_new"]
        missing_merchant_resale += c["missing_merchant_resale"]
        unknown_new += c["unknown_new"]
        unknown_resale += c["unknown_resale"]
        ignored_small_changes += c["ignored_small_changes"]
        api_failures += c["api_failures"]

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

    logger.info("PRICE MONITOR → CYCLE END duration=%.3fs", t_total)
    logger.info("PRICE MONITOR → EVENT LOOP TEST END")


async def republish_published_product(application: Any, published_id: int) -> str:
    """Re-fetch product data and publish again, preserving requested seller_type strictly."""
    db: Database = application.bot_data["db"]
    browser = application.bot_data.get("browser")
    destination_id = application.bot_data.get("destination_channel_id")
    if not destination_id:
        raise RuntimeError("Destination channel not configured")

    row = db.get_published_product(published_id)
    if not row:
        raise RuntimeError("Published product not found")

    asin = row["asin"]
    seller_type = row.get("seller_type") or "NEW_AMAZON"
    merchant_id = AMAZON_RESALE_SELLER_ID if seller_type == "AMAZON_RESALE" else NEW_AMAZON_SELLER_ID

    if seller_type == "AMAZON_RESALE":
        logger.info("RESALE REPUBLISH START published_id=%s asin=%s merchant_id=%s", published_id, asin, merchant_id)
    else:
        logger.info("NEW REPUBLISH START published_id=%s asin=%s merchant_id=%s", published_id, asin, merchant_id)

    clean_url = build_clean_url(asin, AMAZON_DOMAIN, merchant_id=merchant_id if seller_type == "AMAZON_RESALE" else None)
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
            seller_type=seller_type,
        )

        # STRICT SAFETY RULE: Do NOT fallback across seller types
        if product.get("seller_offer_available") is False or product.get("price") == "Not found":
            if seller_type == "AMAZON_RESALE":
                logger.warning("RESALE OFFER MISSING asin=%s action=ABORT_REPUBLISH", asin)
                return "❌ Amazon Resale is currently unavailable"
            else:
                logger.warning("NEW OFFER MISSING asin=%s action=ABORT_REPUBLISH", asin)
                return "❌ NEW Amazon offer is currently unavailable"

        if seller_type == "AMAZON_RESALE":
            logger.info("RESALE OFFER FOUND merchant_id=%s price=%s availability=AVAILABLE", merchant_id, product["price"])

        display_url = resolve_display_url(product, clean_url)
        if seller_type == "AMAZON_RESALE":
            logger.info("RESALE PUBLISH URL display_url=%s", display_url)

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

        # Apply AI rewrite if single product (skip for composite multi-product posts)
        asin_list = [a.strip() for a in asin.split(",") if a.strip()]
        is_composite = len(asin_list) > 1
        apply_ai_rewrite = not is_composite
        reason = "single validated product" if not is_composite else f"{len(asin_list)} validated products"

        logger.info(
            "REPUBLISH → AI REWRITE DECISION\n"
            "  published_id=%s\n"
            "  asin=%s\n"
            "  seller_type=%s\n"
            "  product_count=%s\n"
            "  should_rewrite=%s\n"
            "  reason=%s",
            published_id,
            asin,
            seller_type,
            len(asin_list),
            apply_ai_rewrite,
            reason,
        )

        if apply_ai_rewrite:
            logger.info("REPUBLISH → CALLING AI REWRITE FUNCTION")
            caption = rewrite_caption(caption, db, log_prefix="REPUBLISH")

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
                    seller_type=seller_type,
                )
                db.update_published_product_price_check(published_id, numeric_price)

                if seller_type == "AMAZON_RESALE":
                    logger.info("RESALE PUBLISH SUCCESS published_id=%s asin=%s message_id=%s", published_id, asin, publish_result.message_id)
                else:
                    logger.info("PRICE REPUBLISH success published_id=%s asin=%s message_id=%s", published_id, asin, publish_result.message_id)

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
    product = db.get_published_product_by_asin(asin)
    stats = db.get_price_history_stats(asin, seller_type="NEW_AMAZON")

    if not stats.get("has_data"):
        await query.edit_message_text(
            f"📊 <b>Price History — {asin}</b>\n\nNo price history recorded yet.",
            parse_mode="HTML",
        )
        return

    records = db.get_price_history_records(asin, seller_type="NEW_AMAZON", limit=10)
    ref_new = product.get("new_reference_price") if product else None
    ref_resale = product.get("resale_reference_price") if product else None

    curr_new = stats.get("current_price") or 0.0
    ref_disc_new = ((ref_new - curr_new) / ref_new * 100.0) if (ref_new and ref_new > 0 and curr_new > 0) else 0.0

    lines = [
        f"📊 <b>Price History — {asin}</b>\n",
        f"💰 <b>Current Price (NEW):</b> {format_currency_amount(curr_new)}",
        f"📊 <b>Reference Price (NEW):</b> {format_currency_amount(ref_new) if ref_new else 'N/A'}",
        f"🔥 <b>Reference Discount (NEW):</b> {ref_disc_new:.1f}%",
        f"📉 <b>Last Valid Price (NEW):</b> {format_currency_amount(product.get('new_last_valid_price') or curr_new) if product else 'N/A'}",
        f"🏆 <b>Lowest Price:</b> {format_currency_amount(stats['lowest_price'])}",
        f"📈 <b>Highest Price:</b> {format_currency_amount(stats['highest_price'])}",
        f"📊 <b>Average Price:</b> {format_currency_amount(stats['average_price'])}\n",
    ]

    if ref_resale:
        curr_resale = product.get("resale_last_valid_price") or 0.0
        ref_disc_resale = ((ref_resale - curr_resale) / ref_resale * 100.0) if (ref_resale > 0 and curr_resale > 0) else 0.0
        lines.extend([
            "<b>Amazon Resale:</b>",
            f"💰 <b>Last Valid Price (Resale):</b> {format_currency_amount(curr_resale)}",
            f"📊 <b>Reference Price (Resale):</b> {format_currency_amount(ref_resale)}",
            f"🔥 <b>Reference Discount (Resale):</b> {ref_disc_resale:.1f}%\n",
        ])

    lines.append("<b>Recent Timeline (NEW Amazon):</b>")

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
