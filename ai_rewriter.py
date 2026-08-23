import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def enforce_seller_type_caption_rules(
    caption: str,
    seller_type: str | None = None,
    original_caption: str | None = None,
    seller_condition: str | None = None,
) -> str:
    """
    Enforces strict rules for AMAZON_RESALE vs NEW_AMAZON captions:

    FOR AMAZON_RESALE:
    - MUST explicitly state product is USED ("مستعمل").
    - Condition must be preserved accurately (Using format_resale_condition_arabic).
    - Must NEVER describe as "جديد" or imply unused.

    FOR NEW_AMAZON:
    - MUST NOT add any "مستعمل" or "Resale" wording under any circumstances.
    """
    if not caption:
        return caption

    if seller_type:
        is_resale = (seller_type == "AMAZON_RESALE")
    else:
        orig_text = (original_caption or caption)
        is_resale = (
            "Amazon Resale" in orig_text
            or "مستعمل" in orig_text
            or "Resale" in orig_text
        )

    if is_resale:
        from telegram_publisher import format_resale_condition_arabic

        # Rule 1: Never describe an AMAZON_RESALE product as "جديد" or imply unused.
        caption = re.sub(r'\b(منتج جديد|بحالة جديدة|جديد تماماً|جديد تماما|جديد بالكامل)\b', 'منتج مستعمل', caption, flags=re.IGNORECASE)
        caption = re.sub(r'📦\s*(?:<b>)?الحالة:(?:</b>)?\s*جديد', '📦 الحالة: مستعمل', caption, flags=re.IGNORECASE)

        # Rule 2: Ensure USED / PRE-OWNED ("مستعمل") is explicitly stated in caption.
        if "مستعمل" not in caption:
            condition_phrase = format_resale_condition_arabic(seller_condition)
            if "Amazon Resale" in caption:
                caption = caption.replace("Amazon Resale", f"Amazon Resale\n{condition_phrase}", 1)
            else:
                caption = f"♻️ Amazon Resale\n{condition_phrase}\n\n" + caption

        # Rule 3: Ensure "Amazon Resale" header is present
        if "Amazon Resale" not in caption and "Resale" not in caption:
            caption = "♻️ Amazon Resale\n" + caption
    else:
        # FOR NEW_AMAZON:
        # MUST NOT contain any "مستعمل" or "Resale" wording under any circumstances.
        caption = re.sub(r'♻️\s*(?:<b>)?Amazon Resale.*?(?:</b>)?\n?', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'(?:<b>)?♻️.*?(?:</b>)?\n?', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'♻️\s*المنتج.*?مستعمل\n?', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'📦\s*(?:<b>)?الحالة:(?:</b>)?\s*Used.*?\n?', '', caption, flags=re.IGNORECASE)
        caption = re.sub(r'\b(مستعمل|Amazon Resale)\b', '', caption, flags=re.IGNORECASE)

    return caption


def rewrite_caption(
    caption: str,
    db: Any,
    skip_cache: bool = False,
    log_prefix: str = "",
    seller_type: str | None = None,
    seller_condition: str | None = None,
) -> str:
    """
    Rewrite caption using selected AI provider (Gemini or ChatGPT).
    Enforces strict seller type caption rules (AMAZON_RESALE vs NEW_AMAZON).
    """
    logger.info(f"{log_prefix} → ENTERING AI REWRITE (seller_type={seller_type})")

    # Get selected provider
    provider = db.get_ai_provider()

    # Dispatch to appropriate provider
    raw_rewritten = caption
    if provider == "gemini":
        if not db.get_gemini_enabled():
            logger.info(f"{log_prefix} → GEMINI REWRITE SKIPPED: Gemini disabled")
        else:
            logger.info(f"{log_prefix} → DISPATCHING TO gemini_rewriter")
            from gemini_rewriter import rewrite_caption as gemini_rewrite
            raw_rewritten = gemini_rewrite(caption, db, skip_cache=skip_cache, log_prefix=log_prefix)
    elif provider == "chatgpt":
        if not db.get_chatgpt_rewrite_enabled():
            logger.info(f"{log_prefix} → CHATGPT REWRITE SKIPPED: ChatGPT disabled")
        else:
            if not skip_cache and db.get_chatgpt_skip_cache():
                skip_cache = True
            logger.info(f"{log_prefix} → DISPATCHING TO chatgpt_rewriter")
            from chatgpt_rewriter import rewrite_caption as chatgpt_rewrite
            raw_rewritten = chatgpt_rewrite(caption, db, skip_cache=skip_cache, log_prefix=log_prefix)
    else:
        logger.warning(f"{log_prefix} → Unknown provider: {provider}. Falling back to original caption.")

    final_caption = enforce_seller_type_caption_rules(
        raw_rewritten,
        seller_type=seller_type,
        original_caption=caption,
        seller_condition=seller_condition,
    )
    return final_caption
