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
    DRAFT_PROFILE,
    CreatorsAPIError,
    creators_api_configured,
    get_creators_client,
)
from creators_title import resolve_frame_title
from image_processor import apply_frame, apply_frame_creators_product

logger = logging.getLogger(__name__)

_FALLBACK_BODY_LIMIT = 500


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
            title=title,
            price=price,
            list_price=list_price,
            prime_exclusive=prime_exclusive,
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
    """
    if product.get("data_source") == "creators" and product.get("detail_page_url"):
        return apply_affiliate_tag(product["detail_page_url"])
    return apply_affiliate_tag(clean_url)


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


async def fetch_product(
    db,
    browser: BrowserManager | None,
    asin: str,
    clean_url: str,
    scrape_key: str,
    *,
    coupon_enabled: bool,
    frame_enabled: bool = FRAME_PRODUCT_IMAGES,
) -> dict:
    """
    Fetch product data for drafts and auto posts.

    Returns a dict compatible with existing caption/publish pipelines.
    """
    client = get_creators_client()
    coupon_scan: dict | None = None

    if client and creators_api_configured():
        try:
            items = await client.get_items(
                [asin],
                DRAFT_PROFILE,
                db=db,
                profile="draft",
            )
            item = items.get(asin.upper())
            if item and item.title != "Not found":
                product: dict = {
                    "asin": asin.upper(),
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

                logger.info(
                    "SCRAPER DEBUG title=%r price=%r list_price=%r coupon=%r "
                    "coupon_already_applied=%s source=creators",
                    product["title"],
                    product["price"],
                    product.get("list_price"),
                    product.get("coupon"),
                    product.get("coupon_already_applied"),
                )
                return product
        except RuntimeError:
            raise
        except CreatorsAPIError as exc:
            _log_creators_fallback(asin, exc)
        except Exception:
            logger.exception("CREATORS API FALLBACK — unexpected error")

    # Full Playwright fallback (transparent to user).
    if browser is None:
        raise RuntimeError("Playwright browser not available and Creators API failed")

    logger.info("CREATORS API FALLBACK — full Playwright scrape asin=%s", asin)
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
