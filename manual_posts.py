import logging
import os
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import ADMIN_USER_IDS, FRAME_PRODUCT_IMAGES, LAST_PUBLISHED_LOOKBACK, AMAZON_DOMAIN
from conversation_states import AWAIT_DRAFT_CAPTION
from amazon_scraper import BrowserManager
from amazon_shortener import shorten_amazon_url
from product_fetcher import fetch_product, resolve_display_url
from creators_api import NEW_AMAZON_SELLER_ID, AMAZON_RESALE_SELLER_ID
from database import Database
from duplicate_moderation import send_approval_request
from file_cleanup import cleanup_files
from link_resolver import (
    build_clean_url,
    extract_asin,
    extract_manual_inputs,
    is_amazon_url,
    is_http_url,
    is_manual_post_input,
    is_standalone_asin,
    resolve_and_classify_url,
    resolve_product_input,
    resolve_redirect,
)
from ai_caption import build_product_caption
from creators_title import resolve_frame_title
from image_processor import CreatorsProductCard, apply_frame_creators_products
from composite_builder import (
    fetch_composite_entries,
    build_composite_image,
    build_composite_caption,
    chunk_urls_for_composite,
    get_composite_max_products,
)
from telegram_publisher import (
    build_caption,
    publish_to_channel_with_overflow,
    SAFE_CAPTION_LENGTH,
    build_compact_product_summary,
)
from ai_rewriter import rewrite_caption
from multi_publisher import publish_to_destinations
from upload_prep import prepare_channel_upload
from coupon_price import coupon_apply_kwargs_from_product, normalize_caption_price_line
from inline_buttons import build_inline_keyboard
from published_price import extract_published_price_fields
from promotion_handler import (
    InvalidPromotionUrlError,
    PromotionError,
    PromotionNavigationError,
    PromotionScreenshotError,
    is_promotion_url,
    process_promotion_url,
)
from search_handler import (
    MAX_INLINE_BUTTON_URL_BYTES,
    InvalidSearchUrlError,
    SearchEmptyResultsError,
    SearchError,
    SearchNavigationError,
    SearchScreenshotError,
    is_search_url,
    normalize_search_url,
    process_search_url,
)

logger = logging.getLogger(__name__)

CB_PUBLISH = "publish_draft:"
CB_EDIT = "edit_draft:"
CB_CANCEL = "cancel_draft:"

UD_EDITING_DRAFT = "editing_draft_id"
UD_MANUAL_MODE = "manual_mode"


def _extract_products_from_draft(draft: dict) -> list[dict]:
    """
    Extract product list from draft for inline button generation.

    For single product drafts: extract from asin, title, clean_url
    For composite drafts: parse asins and titles from combined format
    """
    asins = draft.get("asin", "")
    title = draft.get("title", "")
    clean_url = draft.get("clean_url", "")

    # Check if this is a composite draft (multiple ASNs separated by comma)
    if "," in asins:
        asin_list = [a.strip() for a in asins.split(",")]
        title_list = [t.strip() for t in title.split(" | ")]
        return [
            {"title": t, "url": clean_url} for t in title_list
        ]
    else:
        # Single product draft
        return [{"title": title, "url": clean_url}]


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


def _is_promotion_draft(draft: dict) -> bool:
    return (
        draft.get("seller_type") == "PROMOTION"
        or (draft.get("asin") or "").upper().startswith("PROMO:")
        or (draft.get("price") or "").upper().startswith("PROMOTION")
    )


def _is_search_draft(draft: dict) -> bool:
    return (
        draft.get("seller_type") == "SEARCH"
        or (draft.get("asin") or "").upper().startswith("SEARCH:")
        or (draft.get("price") or "").upper() == "SEARCH"
    )


def _draft_asins(draft: dict) -> list[str]:
    if _is_promotion_draft(draft):
        return []
    return [
        part.strip().upper()
        for part in (draft.get("asin") or "").split(",")
        if part.strip()
    ]


def _is_duplicate_draft(db: Database, draft: dict) -> bool:
    if _is_promotion_draft(draft):
        return False
    return any(
        db.is_asin_in_last_published(asin, LAST_PUBLISHED_LOOKBACK)
        for asin in _draft_asins(draft)
    )


def _should_use_composite(inputs: list[str]) -> bool:
    if any(is_promotion_url(item) or is_search_url(item) for item in inputs):
        return False
    return FRAME_PRODUCT_IMAGES and 2 <= len(inputs) <= get_composite_max_products()


async def _build_product_caption_from_data(
    db: Database,
    *,
    product: dict,
    display_url: str,
    coupon_enabled: bool,
) -> str:
    coupon = product.get("coupon") if coupon_enabled else None
    coupon_kwargs = (
        coupon_apply_kwargs_from_product(product) if coupon_enabled else {}
    )
    if product["price"] == "Not found":
        return build_caption(
            product["title"],
            product["price"],
            display_url,
            coupon=coupon,
            coupon_kwargs=coupon_kwargs,
        )
    return await build_product_caption(
        db,
        product["title"],
        product["price"],
        display_url,
        coupon=coupon,
        product=product,
    )


async def resolve_asin_from_input(item: str) -> tuple[str, str] | None:
    """Return (asin, clean_url) from URL, redirect URL, or bare ASIN via central resolver."""
    logger.info("MANUAL INPUT RECEIVED: %s", item)
    resolved = await resolve_product_input(item, AMAZON_DOMAIN)
    if not resolved:
        return None
    logger.info("ASIN EXTRACTED: %s", resolved.asin)
    return resolved.asin, resolved.clean_url


async def prepare_composite_draft_from_inputs(
    application,
    admin_id: int,
    inputs: list[str],
    *,
    message_id: int,
) -> tuple[dict, str] | None:
    """Fetch 2–6 products, render one composite image, and create a single draft."""
    db = application.bot_data["db"]
    browser = application.bot_data["browser"]
    coupon_enabled = db.get_coupon_detection_enabled()

    entries, temp_files = await fetch_composite_entries(
        db,
        browser,
        inputs,
        coupon_enabled=coupon_enabled,
        scrape_key_prefix="manual",
        message_id=message_id,
    )
    if not entries:
        logger.warning(
            "MANUAL COMPOSITE CANCELLED\n"
            "  reason=no valid products"
        )
        return None

    composite_key = (
        f"manual_{admin_id}_{message_id}_composite_{int(time.time())}"
    )
    composite_path = f"{composite_key}_framed.png"
    build_composite_image(entries, composite_path)
    cleanup_files(temp_files)

    caption = await build_composite_caption(db, entries, coupon_enabled)

    asins = ",".join(entry["asin"].upper() for entry in entries)
    combined_title = " | ".join(entry["title"] for entry in entries)

    comp_stype = entries[0].get("seller_type") or entries[0].get("product", {}).get("seller_type") or "NEW_AMAZON"
    draft_id = db.create_draft_post(
        asin=asins,
        title=combined_title,
        price=entries[0]["price"],
        clean_url=entries[0]["clean_url"],
        caption=caption,
        image_path=composite_path,
        created_by=admin_id,
        coupon=None,
        list_price=None,
        seller_type=comp_stype,
    )
    draft = db.get_draft_post(draft_id)
    logger.info("COMPOSITE DRAFT CREATED draft_id=%s asins=%s", draft_id, asins)
    logger.info("DRAFT IMAGE RETAINED path=%s", composite_path)
    return draft, composite_path


async def prepare_promotion_draft_from_input(
    application,
    admin_id: int,
    item: str,
    *,
    scrape_key: str,
) -> tuple[dict, str] | None:
    """Process an Amazon promotion URL and create a draft row.

    Raises PromotionError subclasses so the caller can report the exact cause.
    """
    db = application.bot_data["db"]
    browser = application.bot_data["browser"]
    held_image: str | None = None

    promo_data = await process_promotion_url(
        browser,
        db,
        item,
        scrape_key=scrape_key,
        domain=AMAZON_DOMAIN,
    )
    screenshot_path = promo_data.get("screenshot")
    if not screenshot_path or not os.path.exists(screenshot_path):
        raise PromotionScreenshotError(
            "Promotion screenshot missing after capture"
        )

    held_image = screenshot_path
    try:
        promo_id = promo_data["promotion_id"]
        draft_asin = f"PROMO:{promo_id}"
        price_marker = (
            "PROMOTION_EXPIRED" if promo_data.get("is_expired") else "PROMOTION"
        )
        draft_id = db.create_draft_post(
            asin=draft_asin,
            title=promo_data["title"],
            price=price_marker,
            clean_url=promo_data["short_url"],
            caption=promo_data["caption"],
            image_path=held_image,
            created_by=admin_id,
            coupon=None,
            list_price=None,
            seller_type="NEW_AMAZON",
        )
        draft = db.get_draft_post(draft_id)
        logger.info(
            "PROMOTION DRAFT CREATED draft_id=%s promo_id=%s is_expired=%s title_missing=%s",
            draft_id,
            promo_id,
            promo_data.get("is_expired"),
            promo_data.get("title_missing"),
        )
        logger.info("DRAFT IMAGE RETAINED path=%s", held_image)
        return draft, held_image
    except Exception:
        if held_image:
            cleanup_files([held_image])
        raise


async def prepare_search_draft_from_input(
    application,
    admin_id: int,
    item: str,
    *,
    scrape_key: str,
) -> tuple[dict, str] | None:
    """Process an Amazon search-results URL and create a draft row.

    Raises SearchError subclasses so the caller can report the exact cause.
    """
    db = application.bot_data["db"]
    browser = application.bot_data["browser"]
    held_image: str | None = None

    search_data = await process_search_url(
        browser,
        db,
        item,
        scrape_key=scrape_key,
        domain=AMAZON_DOMAIN,
    )
    screenshot_path = search_data.get("screenshot")
    if not screenshot_path or not os.path.exists(screenshot_path):
        raise SearchScreenshotError(
            "Search screenshot missing after capture"
        )

    held_image = screenshot_path
    try:
        search_key = search_data["search_key"]
        draft_id = db.create_draft_post(
            asin=search_key,
            title=search_data["title"],
            price="SEARCH",
            clean_url=search_data["affiliate_url"],
            caption=search_data["caption"],
            image_path=held_image,
            created_by=admin_id,
            coupon=None,
            list_price=None,
            seller_type="NEW_AMAZON",
        )
        draft = db.get_draft_post(draft_id)
        logger.info(
            "SEARCH DRAFT CREATED draft_id=%s search_key=%s result_count=%s affiliate_url=%s",
            draft_id,
            search_key,
            search_data.get("result_count"),
            search_data.get("affiliate_url"),
        )
        logger.info("DRAFT IMAGE RETAINED path=%s", held_image)
        return draft, held_image
    except Exception:
        if held_image:
            cleanup_files([held_image])
        raise


async def prepare_draft_from_input(
    application,
    admin_id: int,
    item: str,
    *,
    scrape_key: str,
) -> tuple[dict, str] | None:
    """Scrape and create draft row using Resolve-First, Classify-Second. Returns (draft, screenshot_path) or None."""
    db = application.bot_data["db"]
    browser = application.bot_data.get("browser")

    classified = await resolve_and_classify_url(
        item,
        domain=AMAZON_DOMAIN,
        browser=browser,
    )

    if classified.destination_type == "promotion" and classified.resolved_url:
        return await prepare_promotion_draft_from_input(
            application,
            admin_id,
            classified.resolved_url,
            scrape_key=scrape_key,
        )

    if classified.destination_type == "search" and classified.resolved_url:
        return await prepare_search_draft_from_input(
            application,
            admin_id,
            classified.resolved_url,
            scrape_key=scrape_key,
        )

    if classified.destination_type != "product" or not classified.product_resolution:
        logger.warning(
            "MANUAL DRAFT SKIPPED — destination_type=%s reason=%s input=%r",
            classified.destination_type,
            classified.error_reason,
            item,
        )
        return None

    temp_files: list[str] = []
    held_image: str | None = None

    try:
        resolved = classified.product_resolution

        asin = resolved.asin
        clean_url = resolved.clean_url
        seller_type = resolved.seller_type
        merchant_id = resolved.merchant_id or (AMAZON_RESALE_SELLER_ID if seller_type == "AMAZON_RESALE" else NEW_AMAZON_SELLER_ID)

        logger.info(
            "MANUAL DRAFT RESOLUTION:\n"
            "  asin=%s\n"
            "  seller_type=%s\n"
            "  merchant_id=%s\n"
            "  clean_url=%s",
            asin,
            seller_type,
            merchant_id,
            clean_url,
        )

        coupon_enabled = db.get_coupon_detection_enabled()
        product = await fetch_product(
            db,
            browser,
            asin,
            clean_url,
            scrape_key,
            coupon_enabled=coupon_enabled,
            seller_type=seller_type,
        )

        # STRICT SELLER & PRODUCT VALIDATION (EARLY ABORT BEFORE ANY DOWNSTREAM WORK)
        if (
            not product
            or product.get("seller_offer_available") is False
            or product.get("price") == "Not found"
            or not product.get("price")
            or product.get("title") == "Not found"
            or not product.get("title")
        ):
            if seller_type == "AMAZON_RESALE":
                logger.warning(
                    "MANUAL DRAFT ABORTED — Amazon Resale offer missing/OOS asin=%s merchant_id=%s action=ABORT_DRAFT",
                    asin,
                    AMAZON_RESALE_SELLER_ID,
                )
            else:
                logger.warning(
                    "MANUAL DRAFT ABORTED — NEW Amazon offer missing/OOS asin=%s merchant_id=%s action=ABORT_DRAFT",
                    asin,
                    NEW_AMAZON_SELLER_ID,
                )
            if product and product.get("screenshot"):
                cleanup_files([product["screenshot"]])
            return None

        screenshot_path = product.get("screenshot")
        if not screenshot_path or not os.path.exists(screenshot_path):
            logger.error("DRAFT PREPARATION FAILED — screenshot path missing or file does not exist")
            if screenshot_path:
                cleanup_files([screenshot_path])
            return None

        temp_files.append(screenshot_path)

        display_url = resolve_display_url(product, clean_url)
        # Try to shorten the URL using Amazon SiteStripe AFTER seller validation succeeds
        short_url = await shorten_amazon_url(display_url, db)
        if short_url:
            display_url = short_url

        coupon = product.get("coupon") if coupon_enabled else None
        logger.info(
            "CAPTION DEBUG incoming price=%r coupon=%r list_price=%r "
            "coupon_already_applied=%s",
            product.get("price"),
            coupon,
            product.get("list_price"),
            product.get("coupon_already_applied"),
        )
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
            seller_type=seller_type,
        )
        draft = db.get_draft_post(draft_id)
        logger.info("DRAFT CREATED draft_id=%s asin=%s seller_type=%s", draft_id, asin, seller_type)
        logger.info("DRAFT IMAGE RETAINED path=%s", held_image)
        return draft, held_image
    except PromotionError:
        cleanup_files(temp_files)
        if held_image:
            cleanup_files([held_image])
        raise
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
    if _is_search_draft(draft) and is_search_url(draft.get("clean_url", "")):
        raw_clean_url = draft["clean_url"]
        normalized_clean_url = normalize_search_url(raw_clean_url, db=db)
        if normalized_clean_url != raw_clean_url:
            draft = dict(draft)
            draft["clean_url"] = normalized_clean_url
            if raw_clean_url in (draft.get("caption") or ""):
                draft["caption"] = draft["caption"].replace(raw_clean_url, normalized_clean_url)

    duplicate = _is_duplicate_draft(db, draft)
    caption = build_preview_caption(draft, duplicate=duplicate)

    # Build inline keyboard for product buttons and fixed buttons (user-facing)
    products = _extract_products_from_draft(draft)
    button_products = [
        p
        for p in products
        if len((p.get("url") or "").encode("utf-8")) <= MAX_INLINE_BUTTON_URL_BYTES
    ]
    fixed_buttons = db.list_fixed_buttons(enabled_only=True)
    product_buttons_enabled = db.get_product_buttons_enabled()
    fixed_position = db.get_fixed_buttons_position()
    product_layout = db.get_product_button_layout()
    product_template = db.get_product_button_template()
    max_product_buttons = db.get_max_product_buttons()

    user_keyboard = build_inline_keyboard(
        button_products,
        fixed_buttons,
        product_buttons_enabled,
        fixed_buttons_position=fixed_position,
        product_button_layout=product_layout,
        product_button_template=product_template,
        max_product_buttons=max_product_buttons,
    )

    # Build admin action keyboard (separate section)
    admin_keyboard = draft_preview_keyboard(draft["id"])

    # Combine: admin actions first, then separator, then user keyboard
    if user_keyboard.inline_keyboard:
        merged_rows = list(admin_keyboard.inline_keyboard) + [
            [InlineKeyboardButton("─" * 20, callback_data="preview_separator")]
        ] + list(user_keyboard.inline_keyboard)
        merged_keyboard = InlineKeyboardMarkup(merged_rows)
    else:
        merged_keyboard = admin_keyboard

    # Handle caption overflow for preview
    caption_length = len(caption)
    logger.info(
        "CAPTION DEBUG: length=%d limit=%d mode=%s first_100_chars=%s",
        caption_length,
        SAFE_CAPTION_LENGTH,
        "normal" if caption_length <= SAFE_CAPTION_LENGTH else "overflow",
        caption[:100],
    )

    if caption_length > SAFE_CAPTION_LENGTH:
        # Send photo with short caption
        products = _extract_products_from_draft(draft)
        product_count = len(products)
        short_caption = (
            "🔥 أفضل عروض اليوم\n\n"
            f"📦 يحتوي هذا المنشور على {product_count} منتجات.\n"
            "⬇️ التفاصيل الكاملة وروابط الشراء في الرسالة التالية."
        )
        logger.info(
            "CAPTION DEBUG: sending photo with short_caption length=%d",
            len(short_caption),
        )
        with open(draft["image_path"], "rb") as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=short_caption,
                reply_markup=merged_keyboard,
                parse_mode="HTML",
            )
        # Send compact product summary as text message
        compact_summary = build_compact_product_summary(products)
        logger.info(
            "CAPTION DEBUG: sending compact product summary length=%d",
            len(compact_summary),
        )
        await bot.send_message(
            chat_id=chat_id,
            text=compact_summary,
            parse_mode="HTML",
        )
    else:
        # Normal mode - send photo with full caption
        logger.info("CAPTION: length=%d mode=normal", caption_length)
        with open(draft["image_path"], "rb") as photo:
            await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=caption,
                reply_markup=merged_keyboard,
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
    browser = app.bot_data.get("browser") if hasattr(app, "bot_data") else None

    if len(inputs) >= 2:
        resolved_inputs: list[str] = []
        for raw_item in inputs:
            if is_standalone_asin(raw_item):
                resolved_inputs.append(raw_item)
            else:
                classified = await resolve_and_classify_url(
                    raw_item,
                    domain=AMAZON_DOMAIN,
                    browser=browser,
                )
                resolved_inputs.append(classified.resolved_url or raw_item)
        inputs = resolved_inputs

    if _should_use_composite(inputs):
        try:
            prepared = await prepare_composite_draft_from_inputs(
                app,
                user.id,
                inputs,
                message_id=msg.message_id,
            )
        except RuntimeError as exc:
            if "Screenshot generation failed" in str(exc):
                logger.error("COMPOSITE DRAFT PREPARATION FAILED — screenshot missing")
                await msg.reply_text(
                    "❌ Failed to generate the composite product image. Please try again."
                )
                return ConversationHandler.END
            raise
        if not prepared:
            await msg.reply_text(
                "Could not prepare composite draft (scrape failed or invalid input)."
            )
            return ConversationHandler.END
        draft, _ = prepared
        try:
            await send_draft_preview(app.bot, _db(context), msg.chat_id, draft)
        except Exception:
            logger.exception(
                "Composite draft preview send failed draft_id=%s", draft["id"]
            )
            await msg.reply_text(
                f"Preview failed for ASINs <code>{draft['asin']}</code>. Draft kept.",
                parse_mode="HTML",
            )
            return ConversationHandler.END
        logger.info("DRAFT IMAGE RETAINED path=%s", draft["image_path"])
        await msg.reply_text("✅ 1 draft preview(s) sent.")
        return ConversationHandler.END

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
        except InvalidPromotionUrlError as exc:
            logger.warning("INVALID PROMOTION URL: %s", exc)
            await msg.reply_text(
                f"❌ Invalid promotion URL.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except PromotionNavigationError as exc:
            logger.warning("PROMOTION NAVIGATION FAILED: %s (retryable=%s)", exc, getattr(exc, "retryable", False))
            if getattr(exc, "retryable", False):
                status_txt = f" (HTTP {exc.status_code})" if getattr(exc, "status_code", None) else ""
                await msg.reply_text(
                    f"⚠️ Amazon returned temporary error{status_txt}. Request was not marked successful and can be retried.\n<code>{item[:80]}</code>",
                    parse_mode="HTML",
                )
            else:
                await msg.reply_text(
                    f"❌ Failed to open the Amazon promotion page (navigation/page unavailable).\n<code>{item[:80]}</code>",
                    parse_mode="HTML",
                )
            continue
        except PromotionScreenshotError as exc:
            logger.error("PROMOTION SCREENSHOT FAILED: %s", exc)
            await msg.reply_text(
                f"❌ Failed to capture the promotion screenshot. Please try again.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except PromotionError as exc:
            logger.error("PROMOTION PROCESSING FAILED: %s", exc)
            await msg.reply_text(
                f"❌ Failed to process promotion page.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except InvalidSearchUrlError as exc:
            logger.warning("INVALID SEARCH URL: %s", exc)
            await msg.reply_text(
                f"❌ Invalid Amazon search URL.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except SearchEmptyResultsError as exc:
            logger.warning("SEARCH EMPTY RESULTS: %s", exc)
            await msg.reply_text(
                f"❌ Amazon search page returned no product results.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except SearchNavigationError as exc:
            logger.warning("SEARCH NAVIGATION FAILED: %s (retryable=%s)", exc, getattr(exc, "retryable", False))
            if getattr(exc, "retryable", False):
                status_txt = f" (HTTP {exc.status_code})" if getattr(exc, "status_code", None) else ""
                await msg.reply_text(
                    f"⚠️ Amazon search returned temporary error{status_txt}. Request was not marked successful and can be retried.\n<code>{item[:80]}</code>",
                    parse_mode="HTML",
                )
            else:
                await msg.reply_text(
                    f"❌ Failed to open the Amazon search page (navigation/page unavailable).\n<code>{item[:80]}</code>",
                    parse_mode="HTML",
                )
            continue
        except SearchScreenshotError as exc:
            logger.error("SEARCH SCREENSHOT FAILED: %s", exc)
            await msg.reply_text(
                f"❌ Failed to capture the search results screenshot. Please try again.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except SearchError as exc:
            logger.error("SEARCH PROCESSING FAILED: %s", exc)
            await msg.reply_text(
                f"❌ Failed to process search results page.\n<code>{item[:80]}</code>",
                parse_mode="HTML",
            )
            continue
        except RuntimeError as exc:
            if "Screenshot generation failed" in str(exc):
                logger.error("DRAFT PREPARATION FAILED — screenshot missing")
                await msg.reply_text(
                    "❌ Failed to generate the product image. Please try again."
                )
                continue
            raise
        if not prepared:
            seller_type = (
                "AMAZON_RESALE"
                if ("A2N2MP47XAP1MK" in item or "m=A2N2MP47XAP1MK" in item)
                else "NEW_AMAZON"
            )
            if seller_type == "AMAZON_RESALE":
                err_text = "❌ Amazon Resale offer is currently unavailable."
            else:
                err_text = "❌ Amazon New offer is currently unavailable."
            await msg.reply_text(
                f"{err_text}\n<code>{item[:80]}</code>",
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
    image_path = draft.get("image_path")
    if not image_path or not os.path.isfile(image_path) or os.path.getsize(image_path) <= 0:
        await query.edit_message_caption("❌ Draft image file is missing or empty. Draft preserved for retry.")
        return

    try:
        with Image.open(image_path) as img:
            extrema = img.convert("L").getextrema()
            if extrema is not None and extrema[0] == extrema[1]:
                await query.edit_message_caption("❌ Draft image is blank. Operation cancelled; draft preserved for retry.")
                return
    except Exception:
        pass

    publish_path, publish_temp = prepare_channel_upload(draft["image_path"])
    try:
        is_promotion = _is_promotion_draft(draft)
        is_search = _is_search_draft(draft)
        if is_search and is_search_url(draft.get("clean_url", "")):
            raw_clean_url = draft["clean_url"]
            normalized_clean_url = normalize_search_url(raw_clean_url, db=db)
            if normalized_clean_url != raw_clean_url:
                draft = dict(draft)
                draft["clean_url"] = normalized_clean_url
                if raw_clean_url in (draft.get("caption") or ""):
                    draft["caption"] = draft["caption"].replace(raw_clean_url, normalized_clean_url)
        caption = draft["caption"]
        draft_asins = _draft_asins(draft)
        is_composite = len(draft_asins) > 1

        # Check for duplicate ASINs / search keys in last published
        duplicate_asins = []
        if not is_promotion:
            for asin in draft_asins:
                if db.is_asin_in_last_published(asin, LAST_PUBLISHED_LOOKBACK, source="manual"):
                    duplicate_asins.append(asin)

        if duplicate_asins:
            logger.info(
                "MANUAL POST DUPLICATE ASIN DETECTED: %s in last %d published",
                ", ".join(duplicate_asins),
                LAST_PUBLISHED_LOOKBACK,
            )
            # Route to approval workflow for the first duplicate ASIN
            pending_id = db.create_pending_approval(
                asin=duplicate_asins[0],
                title=draft["title"],
                price=draft.get("price", ""),
                clean_url=draft.get("clean_url", ""),
                source_channel_id=draft["created_by"],
                caption=caption,
                image_path=draft["image_path"],
                coupon=draft.get("coupon"),
                list_price=draft.get("list_price"),
                seller_type=draft.get("seller_type", "NEW_AMAZON"),
            )
            await send_approval_request(
                context.application.bot,
                db,
                pending_id,
                duplicate_asins[0],
                draft["title"],
                draft["created_by"],
                draft["image_path"],
                price=draft.get("price", ""),
                coupon=draft.get("coupon"),
                list_price=draft.get("list_price"),
            )
            await query.edit_message_caption(
                f"⚠️ Duplicate ASIN detected: <code>{duplicate_asins[0]}</code>. Sent for approval.",
                parse_mode="HTML",
            )
            if publish_temp:
                cleanup_files([publish_path])
            return

        if (
            not is_promotion
            and not is_search
            and not is_composite
            and draft.get("price")
            and draft["price"] != "Not found"
        ):
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

        # Build inline keyboard for product buttons and fixed buttons
        products = _extract_products_from_draft(draft)
        button_products = [
            p
            for p in products
            if len((p.get("url") or "").encode("utf-8")) <= MAX_INLINE_BUTTON_URL_BYTES
        ]
        fixed_buttons = db.list_fixed_buttons(enabled_only=True)
        product_buttons_enabled = db.get_product_buttons_enabled()
        fixed_position = db.get_fixed_buttons_position()
        product_layout = db.get_product_button_layout()
        product_template = db.get_product_button_template()
        max_product_buttons = db.get_max_product_buttons()
        inline_keyboard = build_inline_keyboard(
            button_products,
            fixed_buttons,
            product_buttons_enabled,
            fixed_buttons_position=fixed_position,
            product_button_layout=product_layout,
            product_button_template=product_template,
            max_product_buttons=max_product_buttons,
        )

        # Determine products for overflow summary
        published_product_count = len(products)

        # Apply AI rewrite if this is a single ASIN draft (not promotions, searches, or composites)
        apply_ai_rewrite = (not is_promotion) and (not is_search) and (published_product_count == 1)
        reason = (
            "promotion post"
            if is_promotion
            else (
                "search results post"
                if is_search
                else (
                    "single validated product"
                    if published_product_count == 1
                    else f"{published_product_count} validated products"
                )
            )
        )
        logger.info(
            "MANUAL POST → AI REWRITE DECISION\n"
            "  draft_asins=%s\n"
            "  validated_products=%s\n"
            "  should_rewrite=%s\n"
            "  reason=%s",
            len(draft_asins),
            published_product_count,
            apply_ai_rewrite,
            reason,
        )
        seller_type = draft.get("seller_type", "NEW_AMAZON")
        if apply_ai_rewrite:
            logger.info("MANUAL POST → CALLING AI REWRITE FUNCTION")
            caption = rewrite_caption(caption, db, log_prefix="MANUAL POST", seller_type=seller_type)

        # Get enabled destinations
        destinations = db.get_enabled_destinations()
        if not destinations:
            await query.edit_message_caption("❌ No enabled destinations configured.")
            if publish_temp:
                cleanup_files([publish_path])
            return

        # Publish to all destinations
        publish_type = (
            "PROMOTION"
            if is_promotion
            else ("SEARCH" if is_search else None)
        )
        result = await publish_to_destinations(
            context.application.bot,
            destinations,
            publish_path,
            caption,
            reply_markup=inline_keyboard if inline_keyboard.inline_keyboard else None,
            products=products,
            publish_type=publish_type,
        )
        result.log_summary()

        if result.successful == 0:
            await query.edit_message_caption("❌ Failed to publish to any destination. Draft preserved for retry.")
            if publish_temp:
                cleanup_files([publish_path])
            return

        if not is_promotion:
            price_fields = extract_published_price_fields(
                draft.get("price") or "",
                draft.get("list_price"),
            )
            for asin in draft_asins:
                for publish_result in result.results:
                    if publish_result.success:
                        db.add_published_product(
                            asin,
                            draft["title"],
                            draft["created_by"],
                            publish_result.message_id,
                            destination_id=publish_result.destination_id,
                            seller_type=draft.get("seller_type") or "NEW_AMAZON",
                            image_path=draft.get("image_path"),
                            clean_url=draft.get("clean_url"),
                            **price_fields,
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
            f"✅ Published to {result.successful}/{result.total} destination(s)",
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
        if publish_temp:
            cleanup_files([publish_path])
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
