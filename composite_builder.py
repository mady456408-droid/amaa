"""Shared composite product builder for Manual Posts and Source Messages."""

import logging
import time

from image_processor import CreatorsProductCard, apply_frame_creators_products
from creators_title import resolve_frame_title
from amazon_shortener import shorten_amazon_url
from product_fetcher import fetch_product, fetch_products, resolve_display_url
from link_resolver import build_clean_url, extract_asin, resolve_redirect, is_standalone_asin
from config import AMAZON_DOMAIN
from file_cleanup import cleanup_files

logger = logging.getLogger(__name__)

_COMPOSITE_MAX_PRODUCTS = 6


async def resolve_asin_from_url(item: str) -> tuple[str, str] | None:
    """Return (asin, clean_url) from URL, redirect URL, or plain ASIN."""
    logger.info("RESOLVING URL: %s", item)

    # Check if input is already a plain ASIN
    asin = is_standalone_asin(item)
    if asin:
        logger.info("PLAIN ASIN DETECTED: %s", asin)
        return asin, build_clean_url(asin, AMAZON_DOMAIN)

    # Try to extract ASIN from URL
    asin = extract_asin(item)
    if asin:
        logger.info("ASIN EXTRACTED: %s", asin)
        return asin, build_clean_url(asin, AMAZON_DOMAIN)

    # Try to resolve redirect
    try:
        final_url = await resolve_redirect(item)
    except Exception:
        logger.exception("REDIRECT RESOLVED: %s -> failed", item)
        return None

    logger.info("REDIRECT RESOLVED: %s -> %s", item, final_url)

    asin = extract_asin(final_url)
    if not asin:
        logger.warning("ASIN extraction failed after redirect: %s", final_url)
        return None

    logger.info("ASIN EXTRACTED: %s", asin)
    return asin, build_clean_url(asin, AMAZON_DOMAIN)


async def fetch_composite_entries(
    db,
    browser,
    urls: list[str],
    *,
    coupon_enabled: bool,
    scrape_key_prefix: str,
    message_id: int,
) -> tuple[list[dict] | None, list[str]]:
    """Fetch products for composite from URLs.

    Returns (entries, temp_files) where temp_files contains raw image paths
    that must be cleaned up by the caller after building the composite.
    
    Fault-tolerant: continues processing remaining URLs if individual URLs fail.
    Only returns None if no entries were successfully fetched.
    
    Uses bulk GetItems API for efficient fetching of multiple products.
    """
    temp_files: list[str] = []
    entries: list[dict] = []
    urls_found = len(urls[:_COMPOSITE_MAX_PRODUCTS])
    urls_resolved = 0
    urls_fetched = 0
    urls_skipped = 0

    try:
        # Phase 1: Resolve all URLs to ASINs and clean URLs
        resolved_pairs: list[tuple[str, str]] = []
        for index, url in enumerate(urls[:_COMPOSITE_MAX_PRODUCTS], start=1):
            try:
                resolved = await resolve_asin_from_url(url)
                if not resolved:
                    logger.warning(
                        "Composite entry #%d skipped — URL resolution failed: %s", index, url
                    )
                    urls_skipped += 1
                    continue

                asin, clean_url = resolved
                resolved_pairs.append((asin, clean_url))
                urls_resolved += 1
            except Exception:
                logger.exception(
                    "Composite entry #%d skipped — URL resolution error: %s", index, url
                )
                urls_skipped += 1
                continue

        if not resolved_pairs:
            logger.warning("Composite aborted — no URLs resolved")
            return None, []

        # Phase 2: Fetch all products using bulk GetItems
        asins = [asin for asin, _ in resolved_pairs]
        clean_urls = {asin: clean_url for asin, clean_url in resolved_pairs}
        scrape_key = f"{scrape_key_prefix}_{message_id}_bulk_{int(time.time())}"

        try:
            products = await fetch_products(
                db,
                browser,
                asins,
                clean_urls,
                scrape_key,
                coupon_enabled=coupon_enabled,
                frame_enabled=False,
            )
        except Exception:
            logger.exception("Bulk product fetch failed, falling back to individual fetches")
            # Fallback to individual fetches if bulk fails
            products = {}
            for asin, clean_url in resolved_pairs:
                try:
                    individual_scrape_key = f"{scrape_key_prefix}_{message_id}_{asin}_{int(time.time())}"
                    product = await fetch_product(
                        db,
                        browser,
                        asin,
                        clean_url,
                        individual_scrape_key,
                        coupon_enabled=coupon_enabled,
                        frame_enabled=False,
                    )
                    products[asin] = product
                except Exception:
                    logger.exception("Individual fetch failed for asin=%s", asin)

        # Phase 3: Process results in original order
        for index, (asin, clean_url) in enumerate(resolved_pairs, start=1):
            product = products.get(asin)
            if not product or product.get("title") == "Not found":
                logger.warning(
                    "Composite entry #%d skipped — product not found for asin=%s", index, asin
                )
                urls_skipped += 1
                if product and product.get("screenshot"):
                    cleanup_files([product["screenshot"]])
                continue

            try:
                display_url = resolve_display_url(product, clean_url)
                short_url = await shorten_amazon_url(display_url, db)
                if short_url:
                    display_url = short_url

                raw_image = product["screenshot"]
                temp_files.append(raw_image)
                frame_title = await resolve_frame_title(
                    asin, product["title"], db=db
                )
                entries.append(
                    {
                        "asin": asin,
                        "title": product["title"],
                        "frame_title": frame_title,
                        "price": product["price"],
                        "list_price": product.get("list_price"),
                        "display_url": display_url,
                        "clean_url": clean_url,
                        "image_path": raw_image,
                        "product": product,
                    }
                )
                urls_fetched += 1

            except Exception:
                logger.exception(
                    "Composite entry #%d skipped — processing error for asin=%s", index, asin
                )
                urls_skipped += 1
                if product.get("screenshot"):
                    cleanup_files([product["screenshot"]])
                continue

        # Log composite summary
        logger.info(
            "Composite Summary: URLs found=%d, Resolved=%d, Fetched=%d, Skipped=%d, Published=%d",
            urls_found, urls_resolved, urls_fetched, urls_skipped, urls_fetched
        )

        # Only return None if no entries were successfully fetched
        if not entries:
            logger.warning("Composite aborted — no valid entries fetched")
            cleanup_files(temp_files)
            return None, []

        return entries, temp_files
    except Exception:
        logger.exception("Composite entry fetch failed — unexpected error")
        cleanup_files(temp_files)
        return None, []


def build_composite_image(entries: list[dict], output_path: str) -> None:
    """Generate composite image from product entries."""
    cards = [
        CreatorsProductCard(
            image_path=entry["image_path"],
            title=entry["frame_title"],
            price=entry["price"],
        )
        for entry in entries
    ]
    apply_frame_creators_products(output_path, cards)


async def build_composite_caption(
    db,
    entries: list[dict],
    coupon_enabled: bool,
) -> str:
    """Generate combined caption from product entries."""
    from coupon_price import coupon_apply_kwargs_from_product
    from ai_caption import build_product_caption
    from telegram_publisher import build_caption

    caption_parts: list[str] = []
    for entry in entries:
        coupon = entry["product"].get("coupon") if coupon_enabled else None
        coupon_kwargs = (
            coupon_apply_kwargs_from_product(entry["product"]) if coupon_enabled else {}
        )
        if entry["product"]["price"] == "Not found":
            caption_parts.append(
                build_caption(
                    entry["product"]["title"],
                    entry["product"]["price"],
                    entry["display_url"],
                    coupon=coupon,
                    coupon_kwargs=coupon_kwargs,
                )
            )
        else:
            caption_parts.append(
                await build_product_caption(
                    db,
                    entry["product"]["title"],
                    entry["product"]["price"],
                    entry["display_url"],
                    coupon=coupon,
                    product=entry["product"],
                )
            )
    return "\n\n".join(caption_parts)


def chunk_urls_for_composite(urls: list[str]) -> list[list[str]]:
    """Chunk URLs into groups of up to _COMPOSITE_MAX_PRODUCTS for composite processing."""
    chunks: list[list[str]] = []
    for i in range(0, len(urls), _COMPOSITE_MAX_PRODUCTS):
        chunks.append(urls[i:i + _COMPOSITE_MAX_PRODUCTS])
    return chunks


def get_composite_max_products() -> int:
    """Return the maximum number of products in a composite."""
    return _COMPOSITE_MAX_PRODUCTS
