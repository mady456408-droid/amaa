import asyncio
import logging
import os
import time

from telegram import Message
from telegram.ext import ApplicationBuilder, filters as tg_filters

from admin_dashboard import build_admin_handlers, refresh_runtime_config
from price_monitoring import build_price_monitoring_handlers
from amazon_scraper import BrowserManager
from amazon_shortener import shorten_amazon_url
from creators_api import init_creators_client, shutdown_creators_client
from product_fetcher import fetch_product, resolve_display_url
from composite_builder import (
    fetch_composite_entries,
    build_composite_image,
    build_composite_caption,
    chunk_urls_for_composite,
    get_composite_max_products,
)
from multi_publisher import publish_to_destinations
from config import (
    ADMIN_USER_IDS,
    AMAZON_DOMAIN,
    BOT_TOKEN,
    DATABASE_PATH,
    DEDUP_MAX_SIZE,
    DEDUP_TTL_SECONDS,
    DESTINATION_CHANNEL_ID,
    FRAME_PRODUCT_IMAGES,
    LAST_PUBLISHED_LOOKBACK,
    PUBLISH_DELAY_SEC,
    SOURCE_CHANNEL_ID,
    TELEGRAM_CONNECT_TIMEOUT,
    TELEGRAM_POOL_TIMEOUT,
    TELEGRAM_READ_TIMEOUT,
    TELEGRAM_WRITE_TIMEOUT,
)
from database import Database
from dedup import TTLCache
from duplicate_moderation import (
    approval_timeout_loop,
    build_moderation_handlers,
    send_approval_request,
)
from file_cleanup import cleanup_files
from coupon_price import coupon_apply_kwargs_from_product
from manual_posts import build_manual_handlers
from link_resolver import (
    build_clean_url,
    close_http_client,
    extract_all_urls_from_message,
    extract_asin,
    get_message_text,
    init_http_client,
    resolve_redirect,
)
from telegram_listener import start_telethon_listener, stop_telethon_listener
from ai_caption import build_product_caption
from telegram_publisher import build_caption, publish_to_channel_with_overflow
from published_price import extract_published_price_fields
from backup_restore import maybe_notify_restore_complete
from inline_buttons import build_inline_keyboard
from gemini_rewriter import rewrite_caption
from upload_prep import to_jpeg_for_telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def validate_and_fetch_url(
    browser: BrowserManager,
    db: Database,
    url: str,
    message_id: int,
    index: int,
) -> dict | None:
    """
    Validate URL and fetch product data without publishing.
    Returns product data dict if successful, None if failed.
    """
    try:
        logger.info("VALIDATING URL %s: %s", index, url)

        asin = extract_asin(url)
        if asin:
            logger.info("ASIN in URL — skip HTTP redirect")
        else:
            final_url = await resolve_redirect(url)
            logger.info("REDIRECT RESOLVED: %s", final_url)
            asin = extract_asin(final_url)

        if not asin:
            logger.warning("URL FAILED — no ASIN for %s", url)
            return None

        logger.info("ASIN FOUND: %s", asin)

        clean_url = build_clean_url(asin, AMAZON_DOMAIN)
        scrape_asin = f"{asin}_{message_id}_{index}"
        coupon_enabled = db.get_coupon_detection_enabled()
        product = await fetch_product(
            db,
            browser,
            asin,
            clean_url,
            scrape_asin,
            coupon_enabled=coupon_enabled,
        )
        display_url = resolve_display_url(product, clean_url)
        # Try to shorten the URL using Amazon SiteStripe
        short_url = await shorten_amazon_url(display_url, db)
        if short_url:
            display_url = short_url

        logger.info(
            "SCRAPE SUCCESS: %r %r coupon=%r",
            product["title"],
            product["price"],
            product.get("coupon"),
        )

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

        # Return all data needed for publishing
        return {
            "asin": asin,
            "clean_url": clean_url,
            "display_url": display_url,
            "product": product,
            "caption": caption,
            "coupon": coupon,
            "coupon_kwargs": coupon_kwargs,
            "message_id": message_id,
            "index": index,
        }

    except Exception:
        logger.exception("URL VALIDATION FAILED: %s", url)
        return None


async def publish_validated_product(
    application,
    db: Database,
    validated_data: dict,
    msg: Message,
    apply_gemini: bool,
) -> bool:
    """
    Publish a previously validated product.
    Returns True if published (not pending approval).
    """
    message_id = msg.message_id
    source_channel_id = msg.chat_id
    temp_files: list[str] = []
    held_for_approval: str | None = None

    try:
        asin = validated_data["asin"]
        clean_url = validated_data["clean_url"]
        display_url = validated_data["display_url"]
        product = validated_data["product"]
        caption = validated_data["caption"]
        coupon = validated_data["coupon"]
        index = validated_data["index"]
        total = 1  # Single product

        logger.info("PUBLISHING VALIDATED PRODUCT %s asin=%s", index, asin)

        # Apply Gemini AI rewrite if enabled and this is a single ASIN source post
        if apply_gemini:
            logger.info("SOURCE POST → CALLING GEMINI REWRITE FUNCTION")
            caption = rewrite_caption(caption, db, log_prefix="SOURCE POST")

        upload_image = to_jpeg_for_telegram(product["screenshot"])
        if upload_image != product["screenshot"]:
            temp_files.append(upload_image)
        framed_image = product["screenshot"]

        # Check if ASIN is in last 10 published globally
        if db.is_asin_in_last_published(asin, LAST_PUBLISHED_LOOKBACK, source="single"):
            logger.info("DUPLICATE ASIN DETECTED: %s in last %d published", asin, LAST_PUBLISHED_LOOKBACK)
            held_for_approval = framed_image
            if held_for_approval in temp_files:
                temp_files.remove(held_for_approval)
            cleanup_files(
                [p for p in temp_files if p != held_for_approval],
            )
            temp_files.clear()

            pending_id = db.create_pending_approval(
                asin=asin,
                title=product["title"],
                price=product["price"],
                clean_url=clean_url,
                source_channel_id=source_channel_id,
                caption=caption,
                image_path=held_for_approval,
                coupon=coupon,
                list_price=product.get("list_price"),
            )
            await send_approval_request(
                application.bot,
                db,
                pending_id,
                asin,
                product["title"],
                source_channel_id,
                held_for_approval,
                price=product["price"],
                coupon=coupon,
                list_price=product.get("list_price"),
            )
            return False

        # Build inline keyboard for product buttons and fixed buttons
        products = [{"title": product["title"], "url": display_url}]
        fixed_buttons = db.list_fixed_buttons(enabled_only=True)
        product_buttons_enabled = db.get_product_buttons_enabled()
        fixed_position = db.get_fixed_buttons_position()
        product_layout = db.get_product_button_layout()
        product_template = db.get_product_button_template()
        max_product_buttons = db.get_max_product_buttons()
        inline_keyboard = build_inline_keyboard(
            products,
            fixed_buttons,
            product_buttons_enabled,
            fixed_buttons_position=fixed_position,
            product_button_layout=product_layout,
            product_button_template=product_template,
            max_product_buttons=max_product_buttons,
        )

        # Get enabled destinations
        destinations = db.get_enabled_destinations()
        if not destinations:
            logger.error("No enabled destinations configured")
            cleanup_files(temp_files)
            return False

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

        price_fields = extract_published_price_fields(
            product["price"],
            product.get("list_price"),
        )

        # Add to published_products for each successful destination
        for publish_result in result.results:
            if publish_result.success:
                db.add_published_product(
                    asin,
                    product["title"],
                    source_channel_id,
                    publish_result.message_id,
                    destination_id=publish_result.destination_id,
                    **price_fields,
                )

        logger.info("PUBLISHED URL %s/%s asin=%s", index, total, asin)
        published_paths = [product["screenshot"], framed_image]
        if upload_image != framed_image:
            published_paths.append(upload_image)
        cleanup_files(published_paths)
        temp_files.clear()
        return True

    except Exception:
        logger.exception("URL PUBLISH FAILED: asin=%s", validated_data.get("asin"))
        return False
    finally:
        if temp_files:
            cleanup_files(list(temp_files))


async def process_single_url(
    application,
    browser: BrowserManager,
    db: Database,
    url: str,
    msg: Message,
    index: int,
    total: int,
    apply_gemini: bool = True,
) -> bool:
    """Process one URL; return True if published (not pending approval)."""
    # Validate and fetch
    validated = await validate_and_fetch_url(
        browser,
        db,
        url,
        msg.message_id,
        index,
    )
    if not validated:
        return False

    # Publish
    return await publish_validated_product(
        application,
        db,
        validated,
        msg,
        apply_gemini,
    )


async def process_composite_urls(
    application,
    browser: BrowserManager,
    db: Database,
    urls: list[str],
    msg: Message,
    chunk_index: int,
    total_chunks: int,
) -> bool:
    """Process 2-6 URLs as a single composite post. Returns True if published."""
    message_id = msg.message_id
    source_channel_id = msg.chat_id
    coupon_enabled = db.get_coupon_detection_enabled()
    temp_files: list[str] = []

    try:
        logger.info("PROCESSING COMPOSITE CHUNK %s/%s with %s URLs", chunk_index, total_chunks, len(urls))

        entries, raw_temp_files = await fetch_composite_entries(
            db,
            browser,
            urls,
            coupon_enabled=coupon_enabled,
            scrape_key_prefix="source",
            message_id=message_id,
        )
        if not entries:
            logger.warning("COMPOSITE CHUNK %s/%s failed — no entries", chunk_index, total_chunks)
            return False

        composite_key = f"source_{message_id}_composite_{chunk_index}_{int(time.time())}"
        composite_path = f"{composite_key}_framed.png"
        build_composite_image(entries, composite_path)
        cleanup_files(raw_temp_files)
        temp_files.append(composite_path)

        caption = await build_composite_caption(db, entries, coupon_enabled)

        # Check for duplicate ASINs in last published
        # COMPOSITE BEHAVIOR: If ANY ASIN in the composite is a duplicate,
        # the entire composite requires approval. This is necessary because:
        # 1. Composite is a single visual unit (one image with all products)
        # 2. Cannot split the composite - would require re-rendering
        # 3. Admin needs full context of what's being published together
        composite_asins = [entry["asin"].upper() for entry in entries]
        duplicate_asins = []
        for asin in composite_asins:
            if db.is_asin_in_last_published(asin, LAST_PUBLISHED_LOOKBACK, source="composite"):
                duplicate_asins.append(asin)

        if duplicate_asins:
            logger.info(
                "COMPOSITE DUPLICATE ASIN DETECTED: %s in last %d published - ENTIRE COMPOSITE requires approval",
                ", ".join(duplicate_asins),
                LAST_PUBLISHED_LOOKBACK,
            )
            # Route to approval workflow for the first duplicate ASIN
            # The entire composite image is sent for admin review
            duplicate_entry = next(e for e in entries if e["asin"].upper() in duplicate_asins)
            pending_id = db.create_pending_approval(
                asin=duplicate_entry["asin"],
                title=duplicate_entry["title"],
                price=duplicate_entry["product"]["price"],
                clean_url=duplicate_entry["clean_url"],
                source_channel_id=source_channel_id,
                caption=caption,
                image_path=composite_path,
                coupon=duplicate_entry["product"].get("coupon") if coupon_enabled else None,
                list_price=duplicate_entry["product"].get("list_price"),
            )
            await send_approval_request(
                application.bot,
                db,
                pending_id,
                duplicate_entry["asin"],
                duplicate_entry["title"],
                source_channel_id,
                composite_path,
                price=duplicate_entry["product"]["price"],
                coupon=duplicate_entry["product"].get("coupon") if coupon_enabled else None,
                list_price=duplicate_entry["product"].get("list_price"),
            )
            cleanup_files(temp_files)
            return False

        upload_image = to_jpeg_for_telegram(composite_path)
        if upload_image != composite_path:
            temp_files.append(upload_image)

        # Build inline keyboard
        products = [{"title": entry["title"], "url": entry["display_url"]} for entry in entries]
        fixed_buttons = db.list_fixed_buttons(enabled_only=True)
        product_buttons_enabled = db.get_product_buttons_enabled()
        fixed_position = db.get_fixed_buttons_position()
        product_layout = db.get_product_button_layout()
        product_template = db.get_product_button_template()
        max_product_buttons = db.get_max_product_buttons()
        inline_keyboard = build_inline_keyboard(
            products,
            fixed_buttons,
            product_buttons_enabled,
            fixed_buttons_position=fixed_position,
            product_button_layout=product_layout,
            product_button_template=product_template,
            max_product_buttons=max_product_buttons,
        )

        # Get enabled destinations
        destinations = db.get_enabled_destinations()
        if not destinations:
            logger.error("No enabled destinations configured")
            cleanup_files(temp_files)
            return False

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

        # Add all products to published_products for each successful destination
        for entry in entries:
            price_fields = extract_published_price_fields(
                entry["product"]["price"],
                entry["product"].get("list_price"),
            )
            for publish_result in result.results:
                if publish_result.success:
                    db.add_published_product(
                        entry["asin"],
                        entry["title"],
                        source_channel_id,
                        publish_result.message_id,
                        destination_id=publish_result.destination_id,
                        **price_fields,
                    )

        logger.info("COMPOSITE PUBLISHED chunk=%s/%s asins=%s", chunk_index, total_chunks, ",".join(e["asin"] for e in entries))
        cleanup_files(temp_files)
        return result.successful > 0
    except Exception:
        logger.exception("COMPOSITE CHUNK FAILED chunk=%s/%s", chunk_index, total_chunks)
        cleanup_files(temp_files)
        return False


async def process_message(application, msg: Message) -> None:
    message_id = msg.message_id
    dedup: TTLCache = application.bot_data["dedup"]
    browser: BrowserManager = application.bot_data["browser"]
    db: Database = application.bot_data["db"]

    # Check if destinations are configured
    destinations = db.get_enabled_destinations()
    if not destinations:
        logger.error("No enabled destinations configured — skipping message_id=%s", message_id)
        return

    msg_key = f"msg:{msg.chat_id}:{message_id}"
    if not dedup.add(msg_key):
        logger.info("Dedup hit: %s — skipping rapid reprocess", msg_key)
        return

    logger.info("PROCESSING MESSAGE message_id=%s", message_id)
    text = get_message_text(msg)
    logger.info("Message text/caption: %r", text[:300] if text else "")

    urls = extract_all_urls_from_message(msg)
    if not urls:
        logger.warning("FOUND 0 URLS — skipping message_id=%s", message_id)
        return

    total = len(urls)
    logger.info("FOUND %s URLS", total)

    published = 0
    t_total = time.perf_counter()

    # Check if composite should be used
    use_composite = FRAME_PRODUCT_IMAGES and 2 <= total <= get_composite_max_products()

    # Determine if Gemini should be applied based on final published count
    # We need to process URLs first to determine how many will actually be published
    # For single URL mode, we can determine this after processing
    # For composite mode, we need to check before processing

    if use_composite:
        logger.info("USING COMPOSITE MODE for %s URLs", total)
        # Composite mode: always multi-product, so no Gemini
        ok = await process_composite_urls(
            application,
            browser,
            db,
            urls,
            msg,
            chunk_index=1,
            total_chunks=1,
        )
        if ok:
            published = 1
    else:
        # Single product mode or too many products - chunk and process individually
        if total > get_composite_max_products():
            logger.info("TOO MANY URLS (%s) - chunking for composite processing", total)
            url_chunks = chunk_urls_for_composite(urls)
            for chunk_index, chunk in enumerate(url_chunks, start=1):
                if len(chunk) >= 2:
                    # Composite chunk - no Gemini
                    ok = await process_composite_urls(
                        application,
                        browser,
                        db,
                        chunk,
                        msg,
                        chunk_index,
                        len(url_chunks),
                    )
                else:
                    # Single URL in chunk - use two-phase approach
                    logger.info("CHUNK %s/%s: PHASE 1 VALIDATING %s URLs", chunk_index, len(url_chunks), len(chunk))
                    validated_products = []
                    for url_index, url in enumerate(chunk, start=1):
                        validated = await validate_and_fetch_url(
                            browser,
                            db,
                            url,
                            message_id,
                            chunk_index * 100 + url_index,  # Unique index
                        )
                        if validated:
                            validated_products.append(validated)

                    resolved_count = len(validated_products)
                    logger.info(
                        "CHUNK %s/%s: PHASE 1 COMPLETE: original_urls=%s resolved_products=%s",
                        chunk_index,
                        len(url_chunks),
                        len(chunk),
                        resolved_count,
                    )

                    # Decide on Gemini based on actual validated count in this chunk
                    apply_gemini = db.get_gemini_enabled() and resolved_count == 1
                    reason = "single validated product" if resolved_count == 1 else f"{resolved_count} validated products"
                    logger.info(
                        "CHUNK %s/%s: GEMINI DECISION\n"
                        "  original_urls=%s\n"
                        "  validated_products=%s\n"
                        "  should_rewrite=%s\n"
                        "  reason=%s",
                        chunk_index,
                        len(url_chunks),
                        len(chunk),
                        resolved_count,
                        apply_gemini,
                        reason,
                    )

                    # Publish validated products
                    logger.info("CHUNK %s/%s: PHASE 2 PUBLISHING %s validated products", chunk_index, len(url_chunks), resolved_count)
                    for validated_index, validated in enumerate(validated_products, start=1):
                        ok = await publish_validated_product(
                            application,
                            db,
                            validated,
                            msg,
                            apply_gemini,
                        )
                        if ok:
                            published += 1
                if chunk_index < len(url_chunks) and PUBLISH_DELAY_SEC > 0:
                    await asyncio.sleep(PUBLISH_DELAY_SEC)
        else:
            # Single product or composite disabled - process individually
            # Two-phase approach:
            # Phase 1: Validate all URLs to determine which will succeed
            # Phase 2: Count successful validations, decide on Gemini, then publish

            logger.info("PHASE 1: VALIDATING %s URLs", total)
            validated_products = []
            for index, url in enumerate(urls, start=1):
                validated = await validate_and_fetch_url(
                    browser,
                    db,
                    url,
                    message_id,
                    index,
                )
                if validated:
                    validated_products.append(validated)

            resolved_count = len(validated_products)
            logger.info(
                "PHASE 1 COMPLETE: original_urls=%s resolved_products=%s",
                total,
                resolved_count,
            )

            # Phase 2: Decide on Gemini based on actual validated count
            apply_gemini = db.get_gemini_enabled() and resolved_count == 1
            reason = "single validated product" if resolved_count == 1 else f"{resolved_count} validated products"
            logger.info(
                "SOURCE POST → GEMINI DECISION\n"
                "  original_urls=%s\n"
                "  validated_products=%s\n"
                "  should_rewrite=%s\n"
                "  reason=%s",
                total,
                resolved_count,
                apply_gemini,
                reason,
            )

            # Phase 3: Publish validated products
            logger.info("PHASE 2: PUBLISHING %s validated products", resolved_count)
            for index, validated in enumerate(validated_products, start=1):
                ok = await publish_validated_product(
                    application,
                    db,
                    validated,
                    msg,
                    apply_gemini,
                )
                if ok:
                    published += 1
                    if index < resolved_count and PUBLISH_DELAY_SEC > 0:
                        await asyncio.sleep(PUBLISH_DELAY_SEC)

            logger.info(
                "SOURCE POST → FINAL: original_urls=%s resolved_products=%s published_products=%s",
                total,
                resolved_count,
                published,
            )

    logger.info("PUBLISHED %s/%s", published, total)
    logger.info(
        "Message message_id=%s done in %.2fs",
        message_id,
        time.perf_counter() - t_total,
    )


async def worker(application) -> None:
    queue: asyncio.Queue = application.bot_data["queue"]
    logger.info("WORKER STARTED — waiting for channel posts")
    try:
        while True:
            msg: Message = await queue.get()
            try:
                logger.info(
                    "WORKER RECEIVED MESSAGE message_id=%s chat_id=%s",
                    msg.message_id,
                    msg.chat_id,
                )
                await process_message(application, msg)
            except Exception:
                logger.exception(
                    "Worker error message_id=%s", getattr(msg, "message_id", "?")
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        logger.info("Worker stopped")
        raise


async def on_startup(application) -> None:
    application.bot_data["ready"] = False

    db = Database(DATABASE_PATH)
    db.seed_from_env(SOURCE_CHANNEL_ID, DESTINATION_CHANNEL_ID)
    application.bot_data["db"] = db
    refresh_runtime_config(application)

    if not ADMIN_USER_IDS:
        logger.warning("ADMIN_USER_IDS is empty — /admin and approvals disabled")

    await init_http_client()
    await init_creators_client()

    browser = BrowserManager()
    await browser.start()
    application.bot_data["browser"] = browser

    application.bot_data["queue"] = asyncio.Queue()
    application.bot_data["worker_task"] = asyncio.create_task(
        worker(application),
        name="channel-post-worker",
    )
    application.bot_data["approval_timeout_task"] = asyncio.create_task(
        approval_timeout_loop(application),
        name="approval-timeout",
    )
    application.bot_data["dedup"] = TTLCache(
        ttl_seconds=DEDUP_TTL_SECONDS,
        max_size=DEDUP_MAX_SIZE,
    )

    await start_telethon_listener(application)

    application.bot_data["ready"] = True
    logger.info(
        "Startup complete — sources=%s destination=%s paused=%s",
        len(application.bot_data.get("active_source_ids", [])),
        application.bot_data.get("destination_channel_id"),
        application.bot_data.get("paused"),
    )
    await maybe_notify_restore_complete(application)


async def on_shutdown(application) -> None:
    application.bot_data["ready"] = False

    for key in ("worker_task", "approval_timeout_task"):
        task = application.bot_data.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    browser: BrowserManager | None = application.bot_data.get("browser")
    if browser:
        await browser.stop()

    await shutdown_creators_client()
    await stop_telethon_listener(application)
    await close_http_client()
    logger.info("Shutdown complete")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT)
        .read_timeout(TELEGRAM_READ_TIMEOUT)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    admin_filter = (
        tg_filters.User(user_id=ADMIN_USER_IDS) if ADMIN_USER_IDS else tg_filters.ALL
    )

    # Group 0: ConversationHandler (admin dashboard, moderation).
    # Active conversation states are always checked first.
    for handler in build_admin_handlers():
        app.add_handler(handler, group=0)
    for handler in build_moderation_handlers():
        app.add_handler(handler, group=0)

    # Group 1: Standalone handlers that only fire when no conversation state is
    # active.  Manual post auto-detection lives here so it never hijacks active
    # dashboard workflows (AI prompt, caption edit, login flow, etc.).
    for handler in build_manual_handlers(admin_filter):
        app.add_handler(handler, group=1)
    for handler in build_price_monitoring_handlers():
        app.add_handler(handler, group=1)

    logger.info("Starting polling (bot API: admin + publish only)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
