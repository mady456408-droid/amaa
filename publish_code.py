"""Publish Code admin feature.

Allows admins to publish Amazon coupon/code posts instantly using the
existing code.png image template.
"""

import logging
import os
import time
import uuid
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

from telegram import Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes, ConversationHandler

from config import ADMIN_USER_IDS
from conversation_states import AWAIT_PUBLISH_CODE
from file_cleanup import cleanup_files
from multi_publisher import publish_to_destinations

logger = logging.getLogger(__name__)

CB_PUBLISH_CODE = "adm:publish_code"

# Configurable constants for yellow code box in code.png (1320x1320 image)
CODE_BOX_X = 285
CODE_BOX_Y = 851
CODE_BOX_WIDTH = 729
CODE_BOX_HEIGHT = 156


def is_admin(user_id: int | None) -> bool:
    """Check if the given user ID belongs to an admin."""
    return user_id is not None and user_id in ADMIN_USER_IDS


def generate_code_image(
    code: str, template_path: str = "code.png", output_path: str | None = None
) -> str:
    """
    Open template image (code.png) and insert coupon code into the yellow/green rectangle.
    
    Center text horizontally and vertically with an automatically calculated font size
    to fit long codes within the placeholder rectangle with appropriate padding.
    Does not modify any other part of the image.
    Saves and returns the generated image path.
    """
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template image not found: {template_path}")

    img = Image.open(template_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Locate font file from local fonts directory
    base_dir = Path(__file__).resolve().parent
    font_candidates = [
        base_dir / "fonts" / "NotoSans-Bold.ttf",
        base_dir / "fonts" / "Cairo.ttf",
        base_dir / "fonts" / "NotoSansArabic-Bold.ttf",
        base_dir / "fonts" / "NotoSans-Regular.ttf",
        "fonts/NotoSans-Bold.ttf",
        "fonts/Cairo.ttf",
        "fonts/NotoSansArabic-Bold.ttf",
        "fonts/NotoSans-Regular.ttf",
    ]
    font_path = None
    for candidate in font_candidates:
        if os.path.exists(candidate):
            font_path = str(candidate)
            break

    placeholder_center_x = CODE_BOX_X + CODE_BOX_WIDTH / 2.0
    placeholder_center_y = CODE_BOX_Y + CODE_BOX_HEIGHT / 2.0

    # Max width and height constraints within the code box (729x156)
    # Safe padding ensures text stays clean, readable, and strictly inside box
    max_w = CODE_BOX_WIDTH - 60  # 669 px
    max_h = CODE_BOX_HEIGHT - 30 # 126 px

    font_size = 100

    while font_size > 12:
        if font_path:
            font = ImageFont.truetype(font_path, font_size)
        else:
            try:
                font = ImageFont.load_default(size=font_size)
            except TypeError:
                font = ImageFont.load_default()

        bbox = draw.textbbox((0, 0), code, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]

        if text_w <= max_w and text_h <= max_h:
            break
        font_size -= 2

    bbox = draw.textbbox((0, 0), code, font=font)

    # Calculate exact visual center position taking font metrics and text bbox into account
    x = placeholder_center_x - (bbox[0] + bbox[2]) / 2.0
    y = placeholder_center_y - (bbox[1] + bbox[3]) / 2.0

    # Draw code in bold black color inside the code box
    draw.text((x, y), code, fill=(0, 0, 0), font=font)

    if not output_path:
        timestamp = int(time.time())
        rand_id = uuid.uuid4().hex[:6]
        output_path = f"temp_code_{timestamp}_{rand_id}.png"

    img.save(output_path, format="PNG")
    return output_path



def build_code_caption(code: str) -> str:
    """Build Telegram caption using the exact required structure."""
    return (
        "كوبون خصم 15% بحد أقصى 150 جنيه على موقع أمازون\n"
        "\n"
        f"كوبون الخصم : {code}\n"
        "================\n"
        "عروض مجمعهالك علي :-\n"
        "\n"
        "- الالكترونيات : https://link.amazon/B0hDZsFnL\n"
        "\n"
        "- الأجهزة المنزلية : https://link.amazon/B0hSlzg9L\n"
        "\n"
        "- البقالة : https://link.amazon/B04X1QLd2\n"
        "================\n"
        "كل اللي في الرسالة دي عروض ومع خصم الكوبون هتكون بأسعار ممتازة ، الكوبون صالح للاستخدام مرة واحدة لكل عميل ويُطبق مع الخصومات الإضافية ، وله عدد محدود من الاستخدام وينفذ سريعاً"
    )


async def start_publish_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Callback handler when admin clicks '📢 Publish Code'."""
    query = update.callback_query
    if query:
        await query.answer()

    user = update.effective_user
    if not is_admin(user.id if user else None):
        if query:
            await query.answer("Unauthorized", show_alert=True)
        elif update.message:
            await update.message.reply_text("Unauthorized")
        return ConversationHandler.END

    if query and query.message:
        await query.message.reply_text("📝 ابعت كود الخصم:")
    elif update.message:
        await update.message.reply_text("📝 ابعت كود الخصم:")

    return AWAIT_PUBLISH_CODE


async def receive_publish_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Message handler when admin sends the coupon code."""
    msg = update.message
    if not msg or not msg.text:
        return AWAIT_PUBLISH_CODE

    user = update.effective_user
    if not is_admin(user.id if user else None):
        await msg.reply_text("Unauthorized")
        return ConversationHandler.END

    raw_text = msg.text.strip()

    # Handle /cancel command
    if raw_text == "/cancel":
        await msg.reply_text("❌ تم إلغاء نشر الكود")
        return ConversationHandler.END

    # Validation: Reject empty codes
    if not raw_text:
        await msg.reply_text("❌ كود غير صالح. يرجى إدخال كود خصم صحيح:")
        return AWAIT_PUBLISH_CODE

    code = raw_text
    logger.info("CODE PUBLISH START code=%s", code)

    db = context.application.bot_data.get("db")
    destinations = db.get_enabled_destinations() if db else []

    if not destinations:
        logger.error("CODE PUBLISH ERROR error_type=NoDestinationsConfigured error=No enabled destinations found")
        await msg.reply_text("❌ قناة النشر غير موجودة أو البوت لا يملك صلاحية الوصول إليها.")
        return ConversationHandler.END

    dest_log_info = [(d.get("title"), d.get("chat_id")) for d in destinations]
    logger.info("CODE PUBLISH DESTINATION RESOLVED: count=%d destinations=%s", len(destinations), dest_log_info)

    image_path = None
    try:
        image_path = generate_code_image(code)
        logger.info("CODE IMAGE GENERATED path=%s", image_path)

        caption = build_code_caption(code)

        result = await publish_to_destinations(
            bot=context.application.bot,
            destinations=destinations,
            image_path=image_path,
            caption=caption,
            reply_markup=None,
            products=None,
            parse_mode="HTML",
            publish_type="CODE",
            source="database",
        )
        result.log_summary()

        if result.successful > 0:
            logger.info("CODE PUBLISH SUCCESS")
            await msg.reply_text("✅ تم نشر الكود بنجاح")
        else:
            failed_errors = [r.error for r in result.results if r.error]
            error_str = " | ".join(failed_errors)
            if "chat not found" in error_str.lower():
                logger.error("CODE PUBLISH ERROR error_type=BadRequest error=Chat not found destinations=%s", dest_log_info)
                await msg.reply_text("❌ قناة النشر غير موجودة أو البوت لا يملك صلاحية الوصول إليها.")
            else:
                logger.error("CODE PUBLISH ERROR error_type=PublishFailed error=%s destinations=%s", error_str, dest_log_info)
                await msg.reply_text("❌ حصل خطأ أثناء نشر الكود")
        return ConversationHandler.END
    except BadRequest as exc:
        if "chat not found" in str(exc).lower():
            logger.error("CODE PUBLISH ERROR error_type=BadRequest error=Chat not found destinations=%s", dest_log_info)
            await msg.reply_text("❌ قناة النشر غير موجودة أو البوت لا يملك صلاحية الوصول إليها.")
        else:
            logger.exception("CODE PUBLISH ERROR error_type=%s error=%s", type(exc).__name__, str(exc))
            await msg.reply_text("❌ حصل خطأ أثناء نشر الكود")
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("CODE PUBLISH ERROR error_type=%s error=%s", type(exc).__name__, str(exc))
        await msg.reply_text("❌ حصل خطأ أثناء نشر الكود")
        return ConversationHandler.END
    finally:
        if image_path:
            cleanup_files([image_path])

