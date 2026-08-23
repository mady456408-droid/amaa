"""
Product fetch orchestration — Creators API first, Playwright fallback.

Coupon ON  → Creators API + Playwright coupon scan (no title/price scrape).
Coupon OFF → Creators API only (no Playwright).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

from affiliate_tag import apply_affiliate_tag
from amazon_image_url import amazon_image_url_candidates
from amazon_scraper import (
    BrowserManager,
    scrape_amazon,
    scrape_coupon_and_screenshot,
)
from config import FRAME_PRODUCT_IMAGES, USER_AGENT
from creators_api import (
    AMAZON_RESALE_SELLER_ID,
    DRAFT_PROFILE,
    NEW_AMAZON_SELLER_ID,
    CreatorsAPIError,
    creators_api_configured,
    extract_seller_offer,
    get_creators_client,
    log_resale_offer_evidence,
)
from creators_title import resolve_frame_title
from image_processor import apply_frame, apply_frame_creators_product

logger = logging.getLogger(__name__)

_FALLBACK_BODY_LIMIT = 500


def _valid_price(text: str | None) -> bool:
    """Check if price is valid (not None, not empty, not 'Not found')."""
    return bool(text) and text.strip() != "Not found"


def _fallback_reason(exc: CreatorsAPIError) -> str:
    if exc.status_code == 403:
        return "HTTP 403 Forbidden"
    if exc.status_code == 429:
        return "HTTP 429 Rate Limited"
    if exc.status_code and exc.status_code >= 500:
        return f"HTTP {exc.status_code} Server Error"
    if exc.status_code and exc.status_code >= 400:
        return f"HTTP {exc.status_code} Client Error"
    return str(exc)


def _log_creators_fallback(asin: str, exc: CreatorsAPIError) -> None:
    """Structured fallback log — execution continues into Playwright unchanged."""
    body = (exc.response_body or "")[:_FALLBACK_BODY_LIMIT]
    logger.warning(
        "CREATORS API FALLBACK:\n"
        "asin=%s\n"
        "reason=%r\n"
        "response_body=%r",
        asin.upper(),
        _fallback_reason(exc),
        body,
    )


def _maybe_apply_creators_frame(
    image_path: str | None,
    output_path: str,
    *,
    asin: str,
    frame_enabled: bool,
    title: str | None = None,
    price: str | None = None,
    list_price: str | None = None,
    prime_exclusive: bool = False,
    seller_name: str | None = None,
    seller_condition: str | None = None,
    seller_type: str = "NEW_AMAZON",
    merchant_id: str | None = None,
) -> str | None:
    """Apply Creators API framing (large FIT + badges) when enabled."""
    if not frame_enabled:
        if image_path and os.path.exists(image_path):
            return image_path
        return None
    if image_path and os.path.exists(image_path):
        return apply_frame_creators_product(
            image_path,
            output_path,
            asin=asin,
            title=title,
            price=price,
            list_price=list_price,
            prime_exclusive=prime_exclusive,
            seller_name=seller_name,
            seller_condition=seller_condition,
            seller_type=seller_type,
            merchant_id=merchant_id,
        )
    logger.warning(
        "FRAME SKIPPED — image missing path=%s asin=%s",
        image_path,
        asin,
    )
    return None


def _maybe_apply_frame(
    screenshot_path: str | None,
    output_path: str,
    *,
    asin: str,
    frame_enabled: bool,
) -> str | None:
    """Apply frame only when enabled and the source screenshot file exists."""
    if not frame_enabled:
        if screenshot_path and os.path.exists(screenshot_path):
            return screenshot_path
        return None
    if screenshot_path and os.path.exists(screenshot_path):
        return apply_frame(screenshot_path, output_path=output_path)
    logger.warning(
        "FRAME SKIPPED — screenshot missing path=%s asin=%s",
        screenshot_path,
        asin,
    )
    return None


def _require_screenshot(path: str | None, *, asin: str) -> str:
    if path and os.path.exists(path):
        return path
    raise RuntimeError(f"Screenshot generation failed for ASIN {asin}")


def resolve_display_url(product: dict, clean_url: str) -> str:
    """
    Always apply affiliate tag to display URLs regardless of data source.
    Ensures merchant_id (e.g. A2N2MP47XAP1MK for Resale) is preserved.
    """
    merchant_id = product.get("merchant_id")
    if not merchant_id and product.get("seller_type") == "AMAZON_RESALE":
        merchant_id = "A2N2MP47XAP1MK"

    url = clean_url
    if product.get("data_source") == "creators" and product.get("detail_page_url"):
        url = product["detail_page_url"]

    if merchant_id and "m=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}m={merchant_id}"

    return apply_affiliate_tag(url)


async def _download_image(url: str, dest_path: str, *, quiet: bool = False) -> bool:
    try:
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            Path(dest_path).write_bytes(resp.content)
        return True
    except Exception:
        if not quiet:
            logger.exception("Failed to download product image from %s", url)
        return False


async def _download_best_amazon_image(
    url: str,
    dest_path: str,
    *,
    asin: str | None = None,
    db=None,
) -> bool:
    """Try cached or highest-resolution Amazon CDN candidates, falling back on failure."""
    candidates: list[str] = []
    seen: set[str] = set()

    if db is not None and asin:
        cached = db.get_creators_image_url(asin)
        if cached:
            candidates.append(cached)
            seen.add(cached)

    for candidate in amazon_image_url_candidates(url):
        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    if not candidates:
        return False

    for index, candidate in enumerate(candidates):
        quiet = index < len(candidates) - 1
        if await _download_image(candidate, dest_path, quiet=quiet):
            if db is not None and asin:
                db.set_creators_image_url(asin, candidate)
            if candidate != url:
                logger.info("CREATORS IMAGE — resolved higher resolution: %s", candidate)
            return True
    return False


async def _resolve_product_image(
    browser: BrowserManager | None,
    *,
    asin: str,
    clean_url: str,
    scrape_key: str,
    image_url: str | None,
    frame_enabled: bool,
    coupon_enabled: bool,
    coupon_scan: dict | None,
    price: str | None = None,
    list_price: str | None = None,
    prime_exclusive: bool = False,
    title: str | None = None,
    seller_name: str | None = None,
    seller_condition: str | None = None,
    seller_type: str = "NEW_AMAZON",
    merchant_id: str | None = None,
    db=None,
) -> str:
    """Return local image path for publish (framed or raw)."""
    base_path = f"{scrape_key}_img.png"

    # Frame disabled — prefer Creators API image (no Playwright when coupon is off).
    if image_url:
        if await _download_best_amazon_image(
            image_url, base_path, asin=asin, db=db
        ):
            if frame_enabled:
                framed = _maybe_apply_creators_frame(
                    base_path,
                    f"{scrape_key}_framed.png",
                    asin=asin,
                    frame_enabled=True,
                    title=title,
                    price=price,
                    list_price=list_price,
                    prime_exclusive=prime_exclusive,
                    seller_name=seller_name,
                    seller_condition=seller_condition,
                    seller_type=seller_type,
                    merchant_id=merchant_id,
                )
                return _require_screenshot(framed, asin=asin)
            return base_path

    # Framed posts need a screenshot; reuse coupon scan capture when available.
    if frame_enabled and browser is not None:
        if coupon_scan and coupon_scan.get("screenshot"):
            raw = coupon_scan["screenshot"]
        elif coupon_enabled:
            logger.info("COUPON SCAN START (screenshot for frame)")
            scan = await scrape_coupon_and_screenshot(
                browser,
                clean_url,
                scrape_key,
                coupon_detection_enabled=True,
                capture_screenshot=True,
            )
            raw = scan.get("screenshot")
        else:
            # Coupon off: screenshot-only pass when API image is missing (Phase 14).
            logger.info("CREATORS API FALLBACK — screenshot only")
            scan = await scrape_coupon_and_screenshot(
                browser,
                clean_url,
                scrape_key,
                coupon_detection_enabled=False,
                capture_screenshot=True,
            )
            raw = scan.get("screenshot")
        if raw and os.path.isfile(raw):
            framed = _maybe_apply_frame(
                raw,
                f"{scrape_key}_framed.png",
                asin=asin,
                frame_enabled=True,
            )
            return _require_screenshot(framed, asin=asin)

    raise RuntimeError(f"No product image available for asin={asin}")


def _merge_coupon_data(product: dict, scan: dict | None) -> None:
    if not scan:
        return
    if scan.get("coupon"):
        product["coupon"] = scan["coupon"]
    if scan.get("coupon_already_applied"):
        product["coupon_already_applied"] = scan["coupon_already_applied"]
    # Playwright list price only when Creators did not provide one.
    if not product.get("list_price") and scan.get("list_price"):
        product["list_price"] = scan["list_price"]


async def fetch_products(
    db,
    browser: BrowserManager | None,
    asins: list[str],
    clean_urls: dict[str, str],
    scrape_key_prefix: str,
    *,
    coupon_enabled: bool,
    frame_enabled: bool = FRAME_PRODUCT_IMAGES,
) -> dict[str, dict]:
    """
    Fetch multiple products using bulk Creators API GetItems requests.

    Args:
        asins: List of ASINs to fetch (1-10, or more with auto-chunking)
        clean_urls: Dict mapping ASIN -> clean_url for each ASIN
        scrape_key_prefix: Prefix for scrape keys (unique per ASIN will be added)
        coupon_enabled: Whether coupon detection is enabled
        frame_enabled: Whether product framing is enabled

    Returns:
        Dict mapping ASIN -> product dict (same format as fetch_product)

    Features:
        - Removes duplicates automatically
        - Preserves input order
        - Ignores invalid ASINs
        - Continues processing if one ASIN fails
        - Automatic chunking for >10 ASINs (max 10 per API request)
        - Single OAuth token for entire batch
        - Performance logging
    """
    import time

    start_time = time.monotonic()
    client = get_creators_client()

    # Normalize and deduplicate ASINs while preserving order
    seen = set()
    unique_asins = []
    for asin in asins:
        normalized = asin.strip().upper()
        if normalized and normalized not in seen:
            seen.add(normalized)
            unique_asins.append(normalized)

    if not unique_asins:
        logger.info("BULK GETITEMS: no valid ASINs provided")
        return {}

    requested = len(unique_asins)
    results: dict[str, dict] = {}
    failed = 0
    api_calls = 0

    # Chunk into groups of 10 (Creators API limit)
    chunk_size = 10
    chunks = [
        unique_asins[i : i + chunk_size]
        for i in range(0, len(unique_asins), chunk_size)
    ]

    logger.info(
        "BULK GETITEMS: requested=%d chunks=%d",
        requested,
        len(chunks),
    )

    for chunk_index, chunk_asins in enumerate(chunks, start=1):
        api_calls += 1
        chunk_results: dict[str, dict] = {}

        # Try Creators API first
        if client and creators_api_configured():
            try:
                items = await client.get_items(
                    chunk_asins,
                    DRAFT_PROFILE,
                    db=db,
                    profile="draft",
                )

                # Process each ASIN in the chunk
                for asin in chunk_asins:
                    item = items.get(asin)
                    if item and item.title != "Not found":
                        # Validate price before proceeding
                        if not _valid_price(item.price):
                            logger.warning(
                                "PRODUCT SKIPPED\n"
                                "  reason=price_not_found\n"
                                "  asin=%s\n"
                                "  raw_price=%r",
                                asin,
                                item.price,
                            )
                            failed += 1
                            continue
                        
                        clean_url = clean_urls.get(asin, "")
                        scrape_key = f"{scrape_key_prefix}_{asin}_{int(time.time() * 1000)}"
                        coupon_scan: dict | None = None

                        product: dict = {
                            "asin": asin,
                            "title": item.title,
                            "price": item.price,
                            "list_price": item.list_price,
                            "image_url": item.image_url,
                            "detail_page_url": item.detail_page_url,
                            "features": item.features,
                            "coupon": None,
                            "coupon_already_applied": False,
                            "data_source": "creators",
                            "screenshot": None,
                        }

                        if coupon_enabled and browser is not None:
                            logger.info("COUPON SCAN START asin=%s", asin)
                            coupon_scan = await scrape_coupon_and_screenshot(
                                browser,
                                clean_url,
                                scrape_key,
                                coupon_detection_enabled=True,
                                capture_screenshot=frame_enabled,
                            )
                            _merge_coupon_data(product, coupon_scan)

                        frame_title = await resolve_frame_title(asin, item.title, db=db)

                        try:
                            product["screenshot"] = await _resolve_product_image(
                                browser,
                                asin=asin,
                                clean_url=clean_url,
                                scrape_key=scrape_key,
                                image_url=item.image_url,
                                frame_enabled=frame_enabled,
                                coupon_enabled=coupon_enabled,
                                coupon_scan=coupon_scan,
                                title=frame_title,
                                price=item.price,
                                list_price=item.list_price,
                                prime_exclusive=item.prime_exclusive,
                                db=db,
                            )
                            chunk_results[asin] = product
                        except RuntimeError as exc:
                            logger.warning(
                                "BULK GETITEMS: image resolution failed for asin=%s: %s",
                                asin,
                                exc,
                            )
                            failed += 1
                        except Exception:
                            logger.exception(
                                "BULK GETITEMS: unexpected error for asin=%s",
                                asin,
                            )
                            failed += 1
                    else:
                        logger.warning(
                            "BULK GETITEMS: item not found for asin=%s",
                            asin,
                        )
                        failed += 1

                logger.info(
                    "BULK GETITEMS: chunk %d/%d returned=%d",
                    chunk_index,
                    len(chunks),
                    len(chunk_results),
                )

            except CreatorsAPIError as exc:
                logger.warning(
                    "BULK GETITEMS: Creators API failed for chunk %d/%d: %s",
                    chunk_index,
                    len(chunks),
                    exc,
                )
                # Fall through to Playwright for this chunk
            except Exception:
                logger.exception(
                    "BULK GETITEMS: unexpected error for chunk %d/%d",
                    chunk_index,
                    len(chunks),
                )
                # Fall through to Playwright for this chunk
        else:
            logger.info("BULK GETITEMS: Creators API not configured, using Playwright")

        # Playwright fallback for ASINs not fetched via Creators API
        if browser is None:
            logger.warning(
                "BULK GETITEMS: Playwright browser not available, skipping failed ASINs in chunk %d",
                chunk_index,
            )
            for asin in chunk_asins:
                if asin not in chunk_results:
                    failed += 1
        else:
            for asin in chunk_asins:
                if asin not in chunk_results:
                    clean_url = clean_urls.get(asin, "")
                    scrape_key = f"{scrape_key_prefix}_{asin}_{int(time.time() * 1000)}"
                    try:
                        logger.info(
                            "BULK GETITEMS: Playwright fallback for asin=%s",
                            asin,
                        )
                        product = await scrape_amazon(
                            browser,
                            clean_url,
                            scrape_key,
                            coupon_detection_enabled=coupon_enabled,
                        )
                        product["data_source"] = "playwright"
                        product["image_url"] = None
                        product["detail_page_url"] = ""
                        product["asin"] = asin
                        
                        # Validate price after Playwright scrape
                        if not _valid_price(product.get("price")):
                            logger.warning(
                                "PRODUCT SKIPPED\n"
                                "  reason=price_not_found\n"
                                "  asin=%s\n"
                                "  raw_price=%r",
                                asin,
                                product.get("price"),
                            )
                            failed += 1
                            continue

                        if frame_enabled and product.get("screenshot"):
                            framed = _maybe_apply_frame(
                                product["screenshot"],
                                f"{scrape_key}_framed.png",
                                asin=asin,
                                frame_enabled=True,
                            )
                            product["screenshot"] = _require_screenshot(framed, asin=asin)
                        else:
                            product["screenshot"] = _require_screenshot(
                                product.get("screenshot"), asin=asin
                            )

                        chunk_results[asin] = product
                    except Exception:
                        logger.exception(
                            "BULK GETITEMS: Playwright fallback failed for asin=%s",
                            asin,
                        )
                        failed += 1

        results.update(chunk_results)

    elapsed_ms = (time.monotonic() - start_time) * 1000
    returned = len(results)

    logger.info(
        "BULK GETITEMS: requested=%d returned=%d failed=%d api_calls=%d time_ms=%.0f",
        requested,
        returned,
        failed,
        api_calls,
        elapsed_ms,
    )

    return results


async def fetch_product(
    db,
    browser: BrowserManager | None,
    asin: str,
    clean_url: str,
    scrape_key: str,
    *,
    coupon_enabled: bool,
    frame_enabled: bool = FRAME_PRODUCT_IMAGES,
    seller_type: str = "NEW_AMAZON",
) -> dict:
    """
    Fetch product data for drafts, auto posts, and republishing.

    Enforces seller_type ('NEW_AMAZON' or 'AMAZON_RESALE') strictly.
    Returns a dict compatible with existing caption/publish pipelines.
    """
    client = get_creators_client()
    coupon_scan: dict | None = None
    target_merchant_id = NEW_AMAZON_SELLER_ID if seller_type == "NEW_AMAZON" else AMAZON_RESALE_SELLER_ID

    if client and creators_api_configured():
        if seller_type == "AMAZON_RESALE":
            logger.debug(
                "RESALE FETCH START\n"
                "  asin=%s\n"
                "  seller_type=AMAZON_RESALE\n"
                "  merchant_id=%s\n"
                "  source=CREATORS_API",
                asin.upper(),
                AMAZON_RESALE_SELLER_ID,
            )
        try:
            cache_hit = bool(db is not None and db.get_creators_cache(asin.upper(), "draft"))

            items = await client.get_items(
                [asin],
                DRAFT_PROFILE,
                db=db,
                profile="draft",
                bypass_cache=False,
            )
            item = items.get(asin.upper())

            if seller_type == "AMAZON_RESALE":
                status, p_text, p_val, l_text, l_val, s_name, s_cond = (
                    extract_seller_offer(item, "AMAZON_RESALE") if item else ("MISSING", None, None, None, None, None, None)
                )
                cached_offer_found = (status == "AVAILABLE")

                logger.debug(
                    "RESALE CACHE CHECK:\n"
                    "  asin=%s\n"
                    "  cache_hit=%s\n"
                    "  merchant_id=%s\n"
                    "  cached_offer_found=%s",
                    asin.upper(),
                    cache_hit,
                    AMAZON_RESALE_SELLER_ID,
                    cached_offer_found,
                )

                if cache_hit and not cached_offer_found:
                    logger.debug(
                        "RESALE CACHE REFRESH:\n"
                        "  asin=%s\n"
                        "  reason=cached_merchant_missing",
                        asin.upper(),
                    )
                    items = await client.get_items(
                        [asin],
                        DRAFT_PROFILE,
                        db=db,
                        profile="draft",
                        bypass_cache=True,
                    )
                    item = items.get(asin.upper())
                    status, p_text, p_val, l_text, l_val, s_name, s_cond = (
                        extract_seller_offer(item, "AMAZON_RESALE") if item else ("MISSING", None, None, None, None, None, None)
                    )

                source_val = "CACHE" if (cache_hit and cached_offer_found) else "LIVE_API"
                raw_cnt = len(getattr(item, "raw_listings", []) or []) if item else 0
                log_resale_offer_evidence(
                    asin=asin,
                    source=source_val,
                    offer_found=(status == "AVAILABLE"),
                    price=p_text,
                    availability=status,
                    raw_listing_count=raw_cnt,
                    module="product_fetcher",
                )
            else:
                status, p_text, p_val, l_text, l_val, s_name, s_cond = (
                    extract_seller_offer(item, seller_type) if item else ("MISSING", None, None, None, None, None, None)
                )

            if not item or item.title == "Not found":
                if seller_type == "AMAZON_RESALE":
                    logger.warning("RESALE OFFER MISSING asin=%s action=ABORT_REPUBLISH", asin.upper())
                else:
                    logger.warning("NEW OFFER MISSING asin=%s action=ABORT_PUBLISH", asin.upper())
                return {
                    "asin": asin.upper(),
                    "title": getattr(item, "title", "Not found") if item else "Not found",
                    "price": "Not found",
                    "seller_type": seller_type,
                    "merchant_id": target_merchant_id,
                    "seller_offer_available": False,
                    "data_source": "creators",
                    "screenshot": None,
                }

            status, p_text, p_val, l_text, l_val, s_name, s_cond = extract_seller_offer(item, seller_type)

            if status != "AVAILABLE" or not p_text or p_text == "Not found":
                if seller_type == "AMAZON_RESALE":
                    logger.warning("RESALE OFFER MISSING asin=%s action=ABORT_REPUBLISH", asin.upper())
                else:
                    logger.warning("NEW OFFER MISSING asin=%s action=ABORT_PUBLISH", asin.upper())
                return {
                    "asin": asin.upper(),
                    "title": item.title,
                    "price": "Not found",
                    "seller_type": seller_type,
                    "merchant_id": target_merchant_id,
                    "seller_offer_available": False,
                    "data_source": "creators",
                    "screenshot": None,
                }

            logger.info(
                "SELLER LIFECYCLE:\n"
                "  stage=PRODUCT_FETCH_COMPLETE\n"
                "  asin=%s\n"
                "  seller_type=%s\n"
                "  merchant_id=%s",
                asin.upper(),
                seller_type,
                target_merchant_id,
            )

            product: dict = {
                "asin": asin.upper(),
                "title": item.title,
                "price": p_text,
                "list_price": l_text or item.list_price,
                "image_url": item.image_url,
                "detail_page_url": item.detail_page_url,
                "features": item.features,
                "seller_name": s_name or item.seller_name,
                "seller_condition": s_cond,
                "seller_type": seller_type,
                "merchant_id": target_merchant_id,
                "seller_offer_available": True,
                "coupon": None,
                "coupon_already_applied": False,
                "data_source": "creators",
                "screenshot": None,
            }

            if coupon_enabled and browser is not None:
                logger.info("COUPON SCAN START asin=%s", asin)
                coupon_scan = await scrape_coupon_and_screenshot(
                    browser,
                    clean_url,
                    scrape_key,
                    coupon_detection_enabled=True,
                    capture_screenshot=frame_enabled,
                )
                _merge_coupon_data(product, coupon_scan)

            frame_title = await resolve_frame_title(asin, item.title, db=db)

            product["screenshot"] = await _resolve_product_image(
                browser,
                asin=asin,
                clean_url=clean_url,
                scrape_key=scrape_key,
                image_url=item.image_url,
                frame_enabled=frame_enabled,
                coupon_enabled=coupon_enabled,
                coupon_scan=coupon_scan,
                title=frame_title,
                price=product["price"],
                list_price=product["list_price"],
                prime_exclusive=item.prime_exclusive,
                seller_name=product["seller_name"],
                seller_condition=product["seller_condition"],
                seller_type=seller_type,
                merchant_id=target_merchant_id,
                db=db,
            )

            logger.info(
                "SCRAPER DEBUG title=%r price=%r list_price=%r coupon=%r "
                "coupon_already_applied=%s seller_name=%r seller_type=%s source=creators",
                product["title"],
                product["price"],
                product.get("list_price"),
                product.get("coupon"),
                product.get("coupon_already_applied"),
                product.get("seller_name"),
                seller_type,
            )
            return product
        except (TypeError, AttributeError, NameError, KeyError, IndexError) as exc:
            logger.error(
                "CREATORS API CODE BUG — unexpected programming error (not an API failure) asin=%s error=%r",
                asin,
                exc,
                exc_info=True,
            )
            raise
        except CreatorsAPIError as exc:
            if seller_type == "AMAZON_RESALE":
                logger.warning("RESALE CREATORS API FAILED\n  error=%s", exc)
            logger.info("CREATORS API FALLBACK reason=creators_api_error error=%s asin=%s", exc, asin)
            _log_creators_fallback(asin, exc)
        except Exception as exc:
            if seller_type == "AMAZON_RESALE":
                logger.warning("RESALE CREATORS API FAILED\n  error=%s", exc)
            logger.info("CREATORS API FALLBACK reason=unexpected_network_or_api_error error=%s asin=%s", exc, asin)
            _log_creators_fallback(asin, exc)

    # Full Playwright fallback (transparent to user).
    if browser is None:
        raise RuntimeError("Playwright browser not available and Creators API failed")

    logger.info("CREATORS API FALLBACK reason=creators_api_unavailable_or_failed asin=%s — starting Playwright scrape", asin)
    product = await scrape_amazon(
        browser,
        clean_url,
        scrape_key,
        coupon_detection_enabled=coupon_enabled,
    )
    product["data_source"] = "playwright"
    product["image_url"] = None
    product["detail_page_url"] = ""
    product["asin"] = asin.upper()
    
    # Validate price after Playwright scrape
    if not _valid_price(product.get("price")):
        logger.warning(
            "PRODUCT SKIPPED\n"
            "  reason=price_not_found\n"
            "  asin=%s\n"
            "  raw_price=%r",
            asin.upper(),
            product.get("price"),
        )
        raise RuntimeError(f"Product price not found for asin={asin}")

    if frame_enabled and product.get("screenshot"):
        framed = _maybe_apply_frame(
            product["screenshot"],
            f"{scrape_key}_framed.png",
            asin=asin,
            frame_enabled=True,
        )
        product["screenshot"] = _require_screenshot(framed, asin=asin)
    else:
        product["screenshot"] = _require_screenshot(
            product.get("screenshot"), asin=asin
        )

    return product
