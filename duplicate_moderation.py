import logging
from html import escape as html_escape

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.ext import CallbackQueryHandler, ContextTypes

from config import ADMIN_USER_IDS, APPROVAL_TIMEOUT_MINUTES, LAST_PUBLISHED_LOOKBACK, AMAZON_DOMAIN
from database import Database
from telegram_publisher import publish_to_channel_with_overflow
from file_cleanup import cleanup_files
from upload_prep import prepare_channel_upload
from affiliate_tag import apply_affiliate_tag
from link_resolver import build_clean_url
from coupon_price import (
    apply_coupon_to_price,
    effective_coupon_for_caption,
    format_arabic_price_line,
    normalize_caption_price_line,
)
from published_price import extract_published_price_fields

logger = logging.getLogger(__name__)

CB_APPROVE = "approve_duplicate:"
CB_REJECT = "reject_duplicate:"


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def source_channel_name(db: Database, channel_id: int) -> str:
    src = db.get_source_by_channel_id(channel_id)
    return src["channel_name"] if src else str(channel_id)


async def send_approval_request(
    bot: Bot,
    db: Database,
    pending_id: int,
    asin: str,
    title: str,
    source_channel_id: int,
    image_path: str,
    *,
    price: str = "",
    coupon: str | None = None,
    list_price: str | None = None,
) -> None:
    name = source_channel_name(db, source_channel_id)
    title_line = f"📦 {title}\n" if title else ""
    price_line = ""
    if price and coupon and db.get_coupon_detection_enabled():
        result = apply_coupon_to_price(
            price,
            coupon,
            list_price_text=list_price,
        )
        effective = effective_coupon_for_caption(coupon, result)
        price_display = format_arabic_price_line(
            price,
            effective,
            debug_path="approval_request",
            list_price_text=list_price,
        )
        price_line = f"{html_escape(price_display)}\n\n"
    text = (
        "⚠️ <b>Duplicate Product Detected</b>\n\n"
        f"{title_line}"
        f"ASIN: <code>{asin}</code>\n"
        f"Source Channel: {name}\n\n"
        f"{price_line}"
        f"🔗 Display link: <code>{html_escape(apply_affiliate_tag(build_clean_url(asin, AMAZON_DOMAIN)))}</code>\n\n"
        "This ASIN was published recently.\n\n"
        "Publish anyway?"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Publish Anyway",
                    callback_data=f"{CB_APPROVE}{pending_id}",
                ),
                InlineKeyboardButton(
                    "❌ Skip",
                    callback_data=f"{CB_REJECT}{pending_id}",
                ),
            ]
        ]
    )

    for admin_id in ADMIN_USER_IDS:
        try:
            with open(image_path, "rb") as photo:
                await bot.send_photo(
                    chat_id=admin_id,
                    photo=photo,
                    caption=text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Failed to send approval request to admin %s", admin_id)

    logger.info("APPROVAL REQUEST SENT pending_id=%s asin=%s", pending_id, asin)


async def publish_and_record(
    application,
    pending: dict,
    *,
    publish_path: str,
) -> Message:
    destination_id = application.bot_data.get("destination_channel_id")
    if not destination_id:
        raise RuntimeError("Destination channel not configured")

    db = application.bot_data["db"]
    caption = pending["caption"]
    if pending.get("price") and pending["price"] != "Not found":
        coupon = pending.get("coupon")
        if not db.get_coupon_detection_enabled():
            coupon = None
        caption = normalize_caption_price_line(
            caption,
            pending["price"],
            coupon,
            debug_path="publish_duplicate",
            list_price_text=pending.get("list_price"),
        )

    # Build product list for overflow summary
    products = [
        {
            "title": pending["title"],
            "url": pending.get("clean_url", ""),
            "price": pending.get("price"),
        }
    ]

    sent = await publish_to_channel_with_overflow(
        application.bot,
        destination_id,
        publish_path,
        caption,
        products=products,
        parse_mode="HTML",
    )
    price_fields = extract_published_price_fields(
        pending.get("price") or "",
        pending.get("list_price"),
    )
    db.add_published_product(
        pending["asin"],
        pending["title"],
        pending["source_channel_id"],
        sent.message_id,
        **price_fields,
    )
    return sent


async def handle_approve_duplicate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    pending_id = int((query.data or "").replace(CB_APPROVE, ""))
    db = _db(context)
    pending = db.get_pending_approval(pending_id)
    if not pending or pending["status"] != "pending":
        await query.edit_message_caption("Already handled or not found.")
        return

    publish_path: str | None = None
    publish_temp = False
    try:
        publish_path, publish_temp = prepare_channel_upload(pending["image_path"])
        await publish_and_record(
            context.application,
            pending,
            publish_path=publish_path,
        )
        if not db.set_pending_status(pending_id, "approved"):
            await query.edit_message_caption("Already handled.")
            cleanup_files([pending["image_path"]])
            if publish_temp and publish_path:
                cleanup_files([publish_path])
            return
        logger.info("ADMIN APPROVED pending_id=%s asin=%s", pending_id, pending["asin"])
        logger.info(
            "PUBLISHED AFTER APPROVAL pending_id=%s asin=%s",
            pending_id,
            pending["asin"],
        )
        await query.edit_message_caption(
            f"✅ Published ASIN <code>{pending['asin']}</code>",
            parse_mode="HTML",
        )
        paths = [pending["image_path"]]
        if publish_temp and publish_path:
            paths.append(publish_path)
        cleanup_files(paths)
    except Exception:
        logger.exception("Approve publish failed pending_id=%s", pending_id)
        await query.answer("Publish failed", show_alert=True)


async def handle_reject_duplicate(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    pending_id = int((query.data or "").replace(CB_REJECT, ""))
    db = _db(context)
    pending = db.get_pending_approval(pending_id)
    if not pending:
        await query.edit_message_caption("Not found.")
        return

    if pending["status"] == "pending":
        db.set_pending_status(pending_id, "rejected")
        cleanup_files([pending["image_path"]])
        logger.info("ADMIN REJECTED pending_id=%s asin=%s", pending_id, pending["asin"])

    await query.edit_message_caption(
        f"❌ Skipped ASIN <code>{pending['asin']}</code>",
        parse_mode="HTML",
    )


async def approval_timeout_loop(application) -> None:
    import asyncio

    while True:
        try:
            await asyncio.sleep(60)
            db: Database = application.bot_data.get("db")
            if not db:
                continue
            expired = db.get_expired_pending_approvals(APPROVAL_TIMEOUT_MINUTES)
            for pending in expired:
                if db.set_pending_status(pending["id"], "auto_rejected"):
                    cleanup_files([pending["image_path"]])
                    logger.info(
                        "AUTO REJECTED pending_id=%s asin=%s",
                        pending["id"],
                        pending["asin"],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Approval timeout loop error")


def build_moderation_handlers() -> list:
    return [
        CallbackQueryHandler(handle_approve_duplicate, pattern=r"^approve_duplicate:\d+$"),
        CallbackQueryHandler(handle_reject_duplicate, pattern=r"^reject_duplicate:\d+$"),
    ]
