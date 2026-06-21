"""Custom Image Post workflow for admins.

Allows admins to send any image + caption, then automatically:
- Apply branded frame to the image
- Detect Amazon URLs in caption
- Convert to affiliate URLs and shorten them
- Replace URLs in caption
- Create draft preview with Publish/Edit/Cancel actions
"""

import logging
import re
from pathlib import Path
from typing import Final

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from config import ADMIN_USER_IDS
from conversation_states import AWAIT_CUSTOM_IMAGE_POST, AWAIT_DRAFT_CAPTION
from affiliate_tag import apply_affiliate_tag
from amazon_shortener import shorten_amazon_url
from image_processor import apply_frame_top_aligned
from link_resolver import extract_all_urls_from_text, is_amazon_url
from database import Database
from file_cleanup import cleanup_files
from telegram_publisher import publish_to_channel
from upload_prep import prepare_channel_upload
import time

UD_EDITING_DRAFT = "editing_draft_id"

logger = logging.getLogger(__name__)

CB_CUSTOM_IMAGE_POST = "adm:custom_image_post"
CB_CUSTOM_PUBLISH = "custom_publish:"
CB_CUSTOM_CANCEL = "custom_cancel:"

UD_CUSTOM_IMAGE_DRAFT = "custom_image_draft_id"


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def _db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["db"]


def custom_image_preview_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Publish",
                    callback_data=f"{CB_CUSTOM_PUBLISH}{draft_id}",
                ),
                InlineKeyboardButton(
                    "✏ Edit Caption",
                    callback_data=f"edit_draft:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data=f"{CB_CUSTOM_CANCEL}{draft_id}",
                ),
            ],
        ]
    )


def build_preview_caption(draft: dict) -> str:
    header = "📝 <b>Custom Image Post Preview</b>\n\n"
    return header + draft["caption"]


async def receive_custom_image_post(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle photo + caption from admin for custom image post."""
    msg = update.message
    user = update.effective_user
    
    if not is_admin(user.id if user else None):
        await msg.reply_text("Unauthorized")
        return
    
    if not msg.photo:
        await msg.reply_text("Please send a photo with your caption.")
        return
    
    caption = msg.caption or ""
    if not caption.strip():
        await msg.reply_text("Please add a caption to your photo.")
        return
    
    logger.info("CUSTOM IMAGE POST REQUEST RECEIVED user_id=%s", user.id)
    
    # Download the photo
    photo = msg.photo[-1]  # Get largest photo
    file = await photo.get_file()
    
    timestamp = int(time.time())
    image_path = f"temp_custom_image_{timestamp}_{msg.message_id}.jpg"
    
    try:
        await file.download_to_drive(image_path)
        logger.info("CUSTOM IMAGE POST IMAGE DOWNLOADED path=%s", image_path)
    except Exception as exc:
        logger.exception("CUSTOM IMAGE POST DOWNLOAD FAILED")
        await msg.reply_text("Failed to download image.")
        return
    
    # Apply frame (top-aligned fitting behavior)
    framed_path = f"temp_custom_image_framed_{timestamp}_{msg.message_id}.png"
    try:
        apply_frame_top_aligned(image_path, output_path=framed_path)
        logger.info("CUSTOM IMAGE POST FRAME APPLIED path=%s", framed_path)
    except Exception as exc:
        logger.exception("CUSTOM IMAGE POST FRAME FAILED")
        await msg.reply_text("Failed to apply frame to image.")
        cleanup_files([image_path])
        return
    
    # Extract and process URLs
    urls = extract_all_urls_from_text(caption)
    amazon_urls = [u for u in urls if is_amazon_url(u)]
    
    logger.info("CUSTOM IMAGE POST URLS FOUND count=%d", len(amazon_urls))
    
    # Process each Amazon URL
    db = _db(context)
    rewritten_caption = caption
    
    for url in amazon_urls:
        # Apply affiliate tag
        tagged_url = apply_affiliate_tag(url)
        
        # Shorten the URL
        short_url = await shorten_amazon_url(tagged_url, db)
        
        if short_url:
            logger.info(
                "CUSTOM IMAGE POST URL SHORTENED old=%s new=%s",
                url,
                short_url,
            )
            # Replace URL in caption
            rewritten_caption = rewritten_caption.replace(url, short_url)
        else:
            logger.warning(
                "CUSTOM IMAGE POST URL SHORTENING FAILED url=%s",
                url,
            )
            # Use tagged URL if shortening failed
            rewritten_caption = rewritten_caption.replace(url, tagged_url)
    
    # Create draft record
    try:
        draft_id = db.create_draft_post(
            asin="CUSTOM",  # Placeholder for custom posts
            title="Custom Image Post",
            price="Custom",
            clean_url="",
            caption=rewritten_caption,
            image_path=framed_path,
            created_by=user.id if user else 0,
            coupon=None,
            list_price=None,
        )
        logger.info("CUSTOM IMAGE POST DRAFT CREATED draft_id=%s", draft_id)
    except Exception as exc:
        logger.exception("CUSTOM IMAGE POST DRAFT CREATION FAILED")
        await msg.reply_text("Failed to create draft.")
        cleanup_files([image_path, framed_path])
        return
    
    # Clean up original downloaded image (keep framed image)
    cleanup_files([image_path])
    
    # Send preview
    draft = db.get_draft_post(draft_id)
    if not draft:
        await msg.reply_text("Failed to retrieve draft.")
        return
    
    try:
        publish_path, publish_temp = prepare_channel_upload(draft["image_path"])
        await msg.reply_photo(
            photo=publish_path,
            caption=build_preview_caption(draft),
            parse_mode="HTML",
            reply_markup=custom_image_preview_keyboard(draft_id),
        )
        if publish_temp and publish_path != draft["image_path"]:
            cleanup_files([publish_path])
        
        context.user_data[UD_CUSTOM_IMAGE_DRAFT] = draft_id
    except Exception as exc:
        logger.exception("CUSTOM IMAGE POST PREVIEW FAILED")
        await msg.reply_text("Failed to send preview.")
        return


async def handle_custom_publish(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle publish button for custom image post."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return
    
    draft_id = int((query.data or "").replace(CB_CUSTOM_PUBLISH, ""))
    db = _db(context)
    draft = db.get_draft_post(draft_id)
    
    if not draft or draft["status"] != "draft":
        await query.edit_message_caption("Draft not found or already handled.")
        return
    
    destination_id = context.application.bot_data.get("destination_channel_id")
    if not destination_id:
        await query.answer("Destination not configured", show_alert=True)
        return
    
    try:
        publish_path, publish_temp = prepare_channel_upload(draft["image_path"])
        await publish_to_channel(
            context.application.bot,
            destination_id,
            publish_path,
            draft["caption"],
        )
        
        db.set_draft_status(draft_id, "published")
        logger.info("CUSTOM IMAGE POST PUBLISHED draft_id=%s", draft_id)
        
        await query.edit_message_caption(
            f"✅ Custom Image Post Published",
        )
        
        # Cleanup
        paths = [draft["image_path"]]
        if publish_temp and publish_path != draft["image_path"]:
            paths.append(publish_path)
        cleanup_files(paths)
        
        context.user_data.pop(UD_CUSTOM_IMAGE_DRAFT, None)
    except Exception as exc:
        logger.exception("CUSTOM IMAGE POST PUBLISH FAILED")
        await query.answer("Publish failed", show_alert=True)


async def handle_custom_cancel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle cancel button for custom image post."""
    query = update.callback_query
    await query.answer()
    user = update.effective_user
    
    if not is_admin(user.id if user else None):
        await query.answer("Unauthorized", show_alert=True)
        return
    
    draft_id = int((query.data or "").replace(CB_CUSTOM_CANCEL, ""))
    db = _db(context)
    draft = db.get_draft_post(draft_id)
    
    if draft:
        db.set_draft_status(draft_id, "cancelled")
        cleanup_files([draft["image_path"]])
        logger.info("CUSTOM IMAGE POST CANCELLED draft_id=%s", draft_id)
    
    await query.edit_message_caption("❌ Custom Image Post Cancelled")
    context.user_data.pop(UD_CUSTOM_IMAGE_DRAFT, None)


def custom_image_state_handlers(admin_filter):
    """Return state handlers for custom image post workflow."""
    return {
        AWAIT_CUSTOM_IMAGE_POST: [
            MessageHandler(
                admin_filter & filters.PHOTO,
                receive_custom_image_post,
            ),
        ],
    }


def custom_image_callback_handlers() -> list:
    """Return callback handlers for custom image post workflow."""
    return [
        CallbackQueryHandler(handle_custom_publish, pattern=r"^custom_publish:\d+$"),
        CallbackQueryHandler(handle_custom_cancel, pattern=r"^custom_cancel:\d+$"),
    ]
