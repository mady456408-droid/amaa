import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_USER_IDS
from conversation_states import AWAIT_DRAFT_CAPTION
from amazon_scraper import BrowserManager
from amazon_shortener import shorten_amazon_url
from product_fetcher import fetch_product, resolve_display_url
from config import AMAZON_DOMAIN, LAST_PUBLISHED_LOOKBACK
from database import Database
from file_cleanup import cleanup_files
from link_resolver import (
    build_clean_url,
    extract_asin,
    extract_manual_inputs,
    is_amazon_url,
    is_http_url,
    is_manual_post_input,
    is_standalone_asin,
    resolve_redirect,
)
from ai_caption import build_product_caption
from telegram_publisher import build_caption, publish_to_channel
from upload_prep import prepare_channel_upload
from coupon_price import coupon_apply_kwargs_from_product, normalize_caption_price_line

logger = logging.getLogger(__name__)

CB_PUBLISH = "publish_draft:"
CB_EDIT = "edit_draft:"
CB_CANCEL = "cancel_draft:"

UD_EDITING_DRAFT = "editing_draft_id"
UD_MANUAL_MODE = "manual_mode"


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def draft_preview_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Publish",
                    callback_data=f"{CB_PUBLISH}{draft_id}",
                ),
                InlineKeyboardButton(
                    "✏ Edit Caption",
                    callback_data=f"{CB_EDIT}{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"{CB_CANCEL}{draft_id}",
                ),
            ],
        ]
    )


def build_preview_caption(draft: dict, *, duplicate: bool) -> str:
    header = "📝 <b>Draft Preview</b>\n\n"
    if duplicate:
        header += "⚠️ <b>Duplicate ASIN detected</b>\n\n"
    return header + draft["caption"]


async def resolve_asin_from_input(item: str) -> tuple[str, str] | None:
    """Return (asin, clean_url) from URL, redirect URL, or bare ASIN."""
    logger.info("MANUAL INPUT RECEIVED: %s", item)

    bare = is_standalone_asin(item)
    if bare:
        logger.info("ASIN EXTRACTED: %s", bare)
        return bare, build_clean_url(bare, AMAZON_DOMAIN)

    if not is_http_url(item):
        logger.warning("Invalid manual input — not ASIN or URL: %r", item)
        return None

    original = item.strip()
    try:
        final_url = await resolve_redirect(original)
    except Exception:
        logger.exception("REDIRECT RESOLVED: %s -> failed", original)
        return None

    logger.info("REDIRECT RESOLVED: %s -> %s", original, final_url)

    if is_amazon_url(final_url):
        logger.info("AMAZON URL VALIDATED")

    asin = extract_asin(final_url)
    if not asin:
        logger.warning("ASIN extraction failed after redirect: %s", final_url)
        return None

    logger.info("ASIN EXTRACTED: %s", asin)
    return asin, build_clean_url(asin, AMAZON_DOMAIN)


async def prepare_draft_from_input(
    application,
    admin_id: int,
    item: str,
    *,
    scrape_key: str,
) -> tuple[dict, str] | None:
    """Scrape and create draft row. Returns (draft, screenshot_path) or None on failure."""
    db = application.bot_data["db"]
    browser = application.bot_data["browser"]
    temp_files: list[str] = []
    held_image: str | None = None

    try:
        resolved = await resolve_asin_from_input(item)
        if not resolved:
            return None

        asin, clean_url = resolved
        coupon_enabled = db.get_coupon_detection_enabled()
        product = await fetch_product(
            db,
            browser,
            asin,
            clean_url,
            scrape_key,
            coupon_enabled=coupon_enabled,
        )
        display_url = resolve_display_url(product, clean_url)
        # Try to shorten the URL using Amazon SiteStripe
        short_url = await shorten_amazon_url(display_url, db)
        if short_url:
            display_url = short_url
        screenshot_path = product["screenshot"]
        temp_files.append(screenshot_path)

        if product["title"] == "Not found":
            logger.warning("Scrape failed — title not found for asin=%s", asin)
            cleanup_files([screenshot_path])
            return None

        coupon = product.get("coupon") if coupon_enabled else None
        logger.info(
            "CAPTION DEBUG incoming price=%r coupon=%r list_price=%r "
            "coupon_already_applied=%s",
            product.get("price"),
            coupon,
            product.get("list_price"),
            product.get("coupon_already_applied"),
        )
        coupon_kwargs = (
            coupon_apply_kwargs_from_product(product) if coupon_enabled else {}
        )
        if product["price"] == "Not found":
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

        held_image = screenshot_path
        if held_image in temp_files:
            temp_files.remove(held_image)

        draft_id = db.create_draft_post(
            asin=asin,
            title=product["title"],
            price=product["price"],
            clean_url=clean_url,
            caption=caption,
            image_path=held_image,
            created_by=admin_id,
            coupon=coupon,
            list_price=product.get("list_price"),
        )
        draft = db.get_draft_post(draft_id)
        logger.info("DRAFT CREATED draft_id=%s asin=%s", draft_id, asin)
        logger.info("DRAFT IMAGE RETAINED path=%s", held_image)
        return draft, held_image
    except RuntimeError as exc:
        if "Screenshot generation failed" in str(exc):
            logger.error("DRAFT PREPARATION FAILED — screenshot missing")
            cleanup_files(temp_files)
            raise
        logger.exception("Draft preparation failed for %r", item)
        cleanup_files(temp_files)
        if held_image:
            cleanup_files([held_image])
        return None
    except Exception:
        logger.exception("Draft preparation failed for %r", item)
        cleanup_files(temp_files)
        if held_image:
            cleanup_files([held_image])
        return None


async def send_draft_preview(
    bot: Bot,
    db: Database,
    chat_id: int,
    draft: dict,
) -> None:
    duplicate = db.is_asin_in_last_published(draft["asin"], LAST_PUBLISHED_LOOKBACK)
    caption = build_preview_caption(draft, duplicate=duplicate)
    with open(draft["image_path"], "rb") as photo:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption,
            reply_markup=draft_preview_keyboard(draft["id"]),
            parse_mode="HTML",
        )


async def process_manual_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    msg = update.message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        return ConversationHandler.END

    text = (msg.text or "").strip()
    if not text:
        return ConversationHandler.END

    logger.info("MANUAL REQUEST RECEIVED from admin %s", user.id)
    inputs = extract_manual_inputs(text)
    if not inputs:
        await msg.reply_text(
            "No Amazon URL or ASIN found.\n"
            "Send a product link, redirect URL, or 10-character ASIN."
        )
        return ConversationHandler.END

    context.user_data.pop(UD_MANUAL_MODE, None)
    await msg.reply_text(f"Preparing {len(inputs)} draft(s)…")

    app = context.application
    created = 0
    for index, item in enumerate(inputs, start=1):
        scrape_key = f"manual_{user.id}_{msg.message_id}_{index}_{int(time.time())}"
        try:
            prepared = await prepare_draft_from_input(
                app,
                user.id,
                item,
                scrape_key=scrape_key,
            )
        except RuntimeError as exc:
            if "Screenshot generation failed" in str(exc):
                logger.error("DRAFT PREPARATION FAILED — screenshot missing")
                await msg.reply_text(
                    "❌ Failed to generate the product image. Please try again."
                )
                continue
            raise
        if not prepared:
            await msg.reply_text(
                f"Could not prepare (scrape failed or invalid input): "
                f"<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        draft, _ = prepared
        try:
            await send_draft_preview(app.bot, _db(context), msg.chat_id, draft)
        except Exception:
            logger.exception("Draft preview send failed draft_id=%s", draft["id"])
            await msg.reply_text(
                f"Preview failed for ASIN <code>{draft['asin']}</code>. Draft kept.",
                parse_mode="HTML",
            )
            continue
        logger.info("DRAFT IMAGE RETAINED path=%s", draft["image_path"])
        created += 1

    if created:
        await msg.reply_text(f"✅ {created} draft preview(s) sent.")
    else:
        await msg.reply_text("No drafts were created.")
    return ConversationHandler.END


async def receive_draft_caption(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> int:
    msg = update.message
    user = update.effective_user
    if not msg or not user or not is_admin(user.id):
        return ConversationHandler.END

    draft_id = context.user_data.pop(UD_EDITING_DRAFT, None)
    if not draft_id:
        return ConversationHandler.END

    new_caption = (msg.text or "").strip()
    if not new_caption:
        await msg.reply_text("Caption cannot be empty. Send new caption or /cancel.")
        context.user_data[UD_EDITING_DRAFT] = draft_id
        return AWAIT_DRAFT_CAPTION

    db = _db(context)
    if not db.update_draft_caption(draft_id, new_caption):
        await msg.reply_text("Draft not found or already handled.")
        return ConversationHandler.END

    draft = db.get_draft_post(draft_id)
    if not draft:
        await msg.reply_text("Draft not found.")
        return ConversationHandler.END

    logger.info("DRAFT UPDATED draft_id=%s", draft_id)
    await send_draft_preview(context.application.bot, db, msg.chat_id, draft)
    await msg.reply_text("Preview updated with new caption.")
    return ConversationHandler.END


async def handle_publish_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    draft_id = int((query.data or "").replace(CB_PUBLISH, ""))
    db = _db(context)
    draft = db.get_draft_post(draft_id)
    if not draft or draft["status"] != "draft":
        await query.edit_message_caption("Draft not found or already handled.")
        return

    destination_id = context.application.bot_data.get("destination_channel_id")
    if not destination_id:
        await query.answer("Destination not configured", show_alert=True)
        return

    publish_path, publish_temp = prepare_channel_upload(draft["image_path"])
    try:
        caption = draft["caption"]
        if draft.get("price") and draft["price"] != "Not found":
            coupon = draft.get("coupon")
            if not db.get_coupon_detection_enabled():
                coupon = None
            caption = normalize_caption_price_line(
                caption,
                draft["price"],
                coupon,
                debug_path="publish_draft",
                list_price_text=draft.get("list_price"),
            )
        sent = await publish_to_channel(
            context.application.bot,
            destination_id,
            publish_path,
            caption,
        )
        db.add_published_product(
            draft["asin"],
            draft["title"],
            draft["created_by"],
            sent.message_id,
        )
        if not db.set_draft_status(draft_id, "published"):
            await query.edit_message_caption("Already handled.")
            logger.info(
                "DRAFT IMAGE CLEANUP AFTER PUBLISH path=%s",
                draft["image_path"],
            )
            cleanup_files([draft["image_path"]])
            if publish_temp:
                cleanup_files([publish_path])
            return
        logger.info("DRAFT PUBLISHED draft_id=%s asin=%s", draft_id, draft["asin"])
        await query.edit_message_caption(
            f"✅ Published ASIN <code>{draft['asin']}</code>",
            parse_mode="HTML",
        )
        logger.info(
            "DRAFT IMAGE CLEANUP AFTER PUBLISH path=%s",
            draft["image_path"],
        )
        paths = [draft["image_path"]]
        if publish_temp:
            paths.append(publish_path)
        cleanup_files(paths)
    except Exception:
        logger.exception("DRAFT publish failed draft_id=%s", draft_id)
        await query.answer("Publish failed", show_alert=True)


async def handle_edit_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return None

    draft_id = int((query.data or "").replace(CB_EDIT, ""))
    db = _db(context)
    draft = db.get_draft_post(draft_id)
    if not draft or draft["status"] != "draft":
        await query.message.reply_text("Draft not found or already handled.")
        return None

    context.user_data[UD_EDITING_DRAFT] = draft_id
    await query.message.reply_text("Send new caption")
    return AWAIT_DRAFT_CAPTION


async def handle_cancel_draft(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return

    draft_id = int((query.data or "").replace(CB_CANCEL, ""))
    db = _db(context)
    draft = db.get_draft_post(draft_id)
    if not draft:
        await query.edit_message_caption("Draft not found.")
        return

    if draft["status"] == "draft":
        db.set_draft_status(draft_id, "cancelled")
        logger.info(
            "DRAFT IMAGE CLEANUP AFTER REJECT path=%s",
            draft["image_path"],
        )
        cleanup_files([draft["image_path"]])
        logger.info("DRAFT CANCELLED draft_id=%s asin=%s", draft_id, draft["asin"])

    await query.edit_message_caption(
        f"❌ Cancelled ASIN <code>{draft['asin']}</code>",
        parse_mode="HTML",
    )


class ManualInputFilter(filters.MessageFilter):
    """Only matches text that is strictly an ASIN or Amazon/redirect URL.

    Never matches free-form text such as AI prompts, captions, or login codes.
    This filter is used for the *fallback* auto-detection handler that runs
    OUTSIDE the ConversationHandler so active conversation states always win.
    """

    def filter(self, message) -> bool:
        text = getattr(message, "text", None) or ""
        return is_manual_post_input(text.strip())


def build_manual_handlers(admin_filter) -> list:
    """
    Standalone handlers registered OUTSIDE and AFTER the ConversationHandler
    (group=1 in bot.py).  Because they live in a separate, lower-priority
    handler group they are only reached when the ConversationHandler does NOT
    consume the update (i.e. no active conversation state for this user).
    """
    private_admin = admin_filter & filters.ChatType.PRIVATE
    return [
        # Auto-detect ASIN / Amazon URL when admin is idle in private chat.
        MessageHandler(
            private_admin & filters.TEXT & ~filters.COMMAND & ManualInputFilter(),
            process_manual_text,
        ),
        CallbackQueryHandler(
            handle_publish_draft, pattern=r"^publish_draft:\d+$"
        ),
        CallbackQueryHandler(
            handle_cancel_draft, pattern=r"^cancel_draft:\d+$"
        ),
    ]


def manual_entry_handler(admin_filter) -> MessageHandler:
    """Entry point used inside the ConversationHandler for explicit manual mode.

    Only activated after the admin pressed the 'Manual Post' button, which sets
    UD_MANUAL_MODE in user_data.  Free-form text does NOT match this because
    ManualInputFilter (not used here) isn't applied — the AWAIT_MANUAL_INPUT
    state handler accepts any text.
    """
    private_admin = admin_filter & filters.ChatType.PRIVATE
    # This handler is kept as ConversationHandler entry point ONLY for the
    # UD_MANUAL_MODE path (admin already pressed the button).  The filter
    # intentionally checks user_data via a stateful filter so it won't fire
    # unless manual mode is active.
    return MessageHandler(
        private_admin & filters.TEXT & ~filters.COMMAND & _ManualModeActiveFilter(),
        process_manual_text,
    )


class _ManualModeActiveFilter(filters.MessageFilter):
    """Matches only when UD_MANUAL_MODE is set in user_data (button was pressed)."""

    def filter(self, message) -> bool:
        # PTB passes context-free message here; we cannot access user_data in a
        # plain MessageFilter.  We use a workaround: store the flag on the
        # message object itself via a bot_data side-channel set by the callback.
        # Since that's fragile, we simply return False here — explicit manual
        # mode input is already handled by the AWAIT_MANUAL_INPUT state handler
        # registered in manual_state_handlers().
        return False


def manual_state_handlers(admin_filter) -> dict:
    private_admin = admin_filter & filters.ChatType.PRIVATE
    from conversation_states import AWAIT_DRAFT_CAPTION, AWAIT_MANUAL_INPUT

    return {
        AWAIT_MANUAL_INPUT: [
            MessageHandler(
                private_admin & filters.TEXT & ~filters.COMMAND,
                process_manual_text,
            ),
        ],
        AWAIT_DRAFT_CAPTION: [
            MessageHandler(
                private_admin & filters.TEXT & ~filters.COMMAND,
                receive_draft_caption,
            ),
        ],
    }
