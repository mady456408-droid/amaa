"""Dedicated handler for Amazon promotion-page (/promotion/psp/) URLs.

Provides:
1. Promotion URL detection, normalization, and validation.
2. Shared-browser Playwright rendering and screenshot capture.
3. Multi-tier promotion title extraction and expired-status detection.
4. Affiliate tag preservation and Amazon SiteStripe URL shortening with fallback.
5. Telegram caption formatting and publishing helpers with temp-file cleanup.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from PIL import Image

from affiliate_tag import apply_affiliate_tag
from amazon_scraper import BrowserManager
from amazon_shortener import shorten_amazon_url
from config import AMAZON_DOMAIN, USER_AGENT
from file_cleanup import cleanup_files
from inline_buttons import build_inline_keyboard
from multi_publisher import publish_to_destinations
from upload_prep import to_jpeg_for_telegram

logger = logging.getLogger(__name__)

_ACCEPT_LANGUAGE = "ar-EG,ar;q=0.9,en;q=0.8"

FALLBACK_PROMOTION_TITLE = "عرض ترويجي من أمازون"

# Navigation retry constants
MAX_NAVIGATION_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.5
MAX_BACKOFF_SECONDS = 8.0
BACKOFF_JITTER_SECONDS = 0.5
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_nav_sleep = asyncio.sleep

# Sensible viewport suitable for reading Arabic and English promotion pages
PROMOTION_VIEWPORT_WIDTH = 1366
PROMOTION_VIEWPORT_HEIGHT = 900

# Maximum full-page / cropped height before capping to avoid excessive height
MAX_FULL_PAGE_HEIGHT = 2400

# Minimum cropped height when capping long promotion pages so the title, filters, and first products stay visible
MIN_PROMOTION_CLIP_HEIGHT = 550

# Injected CSS to hide Amazon header, navigation, footer, cookie banners, and sticky UI before screenshot
PROMOTION_SCREENSHOT_CSS = """
#navbar,
#navbar-main,
header#navbar-main,
#nav-main,
#nav-belt,
#nav-subnav,
#nav-progressive-subnav,
#nav-upnav,
#skiplink,
#gw-desktop-herotator,
#navFooter,
#footer,
footer,
#navBackToTop,
#rhf,
#rhf-container,
.rhf-frame,
#sp-cc,
#sp-cc-banner,
.sp-cc-banner,
#glow-toaster,
.glow-toaster,
#nav-flyout-iss-anchor,
#nav-flyout-anchor {
    display: none !important;
}

html, body {
    margin-top: 0 !important;
    padding-top: 0 !important;
    margin-bottom: 0 !important;
    padding-bottom: 0 !important;
}
"""

# Candidate selectors that wrap the entire promotion content (title, date range, filters, and product grid)
PROMOTION_CONTAINER_SELECTORS = (
    "#promotionPage",
    "#psp-main-container",
    "#psp-container",
    ".psp-main-container",
    "[data-csa-c-content-id*='promotion']",
)

# Candidate selectors for individual promotion sections used to compute a union bounding box
PROMOTION_SECTION_SELECTORS = (
    "#promotionHeader",
    "#promotionTitle",
    "#promotionSchedule",
    "#promotionDescription",
    "#promotionDetails",
    "#promotionFilter",
    "#promotionFilters",
    ".psp-header",
    ".psp-filter-container",
    "#promotionProductList",
    "#mainResults",
    ".psp-product-list",
    ".psp-product-grid",
    "[id*='promotion']",
    "[class*='psp']",
    "main",
)

_AMAZON_HOST_RE = re.compile(
    r"(?:^|\.)amazon\.(?:com|co\.uk|de|fr|it|es|ca|com\.au|com\.br|co\.jp|in|eg|sa|ae|nl|se|pl|sg|tr|mx)$",
    re.IGNORECASE,
)

# Matches /promotion/psp/ in path (with optional language prefix like /-/ar/ or /-/en/)
_PROMO_PATH_MARKER_RE = re.compile(r"/promotion/psp(?:/|$)", re.IGNORECASE)

# Extracts the promotion ID after /promotion/psp/
_PROMO_ID_RE = re.compile(r"/promotion/psp/([A-Za-z0-9]{4,32})(?:[/?#]|$)", re.IGNORECASE)

# Irrelevant UI phrases to strip from extracted promotion titles
_IRRELEVANT_UI_PATTERNS = [
    r"\bSee\s+details\b",
    r"\bTerms\s+and\s+Conditions\b",
    r"\bTerms\s*&\s*Conditions\b",
    r"\bShop\s+now\b",
    r"\bContinue\s+shopping\b",
    r"\bSkip\s+to\s+main\s+content\b",
    r"الشروط\s+والأحكام",
    r"تطبق\s+الشروط\s+والأحكام",
    r"تطبق\s+الشروط",
    r"عرض\s+التفاصيل",
    r"التفاصيل",
    r"تسوق\s+الآن",
    r"متابعة\s+التسوق",
    r"الانتقال\s+إلى\s+المحتوى\s+الرئيسي",
]

# Generic Amazon branding patterns to remove from document title / metadata
_BRANDING_PATTERNS = [
    r"^\s*Amazon\.(?:eg|com|sa|ae|co\.uk|de|fr)\s*[:|\-–—]\s*",
    r"\s*[:|\-–—]\s*Amazon\.(?:eg|com|sa|ae|co\.uk|de|fr)\s*$",
    r"^\s*أمازون\s+مصر\s*[:|\-–—]\s*",
    r"\s*[:|\-–—]\s*أمازون\s+مصر\s*$",
    r"^\s*أمازون\s*[:|\-–—]\s*",
    r"\s*[:|\-–—]\s*أمازون\s*$",
    r"^\s*Amazon\s*[:|\-–—]\s*",
    r"\s*[:|\-–—]\s*Amazon\s*$",
]

# Generic non-informative titles that should not be accepted as a specific promotion name
_GENERIC_TITLES = {
    "amazon",
    "amazon.eg",
    "amazon.com",
    "amazon egypt",
    "أمازون",
    "أمازون مصر",
    "امازون",
    "امازون مصر",
    "promotion",
    "promotions",
    "amazon promotion",
    "shop all",
    "عروض أمازون",
    "صفحة العرض",
    "قائمة التصفح",
    "تسجيل الدخول",
    "sign in",
    "page not found",
}

# Explicit markers indicating a promotion has ended / expired
_EXPIRED_PROMOTION_MARKERS = (
    "this promotion has ended",
    "promotion has ended",
    "the promotion has ended",
    "this promotion is no longer available",
    "promotion has expired",
    "this offer has ended",
    "this offer has expired",
    "offer has ended",
    "promotion ended",
    "انتهى هذا العرض الترويجي",
    "انتهى العرض الترويجي",
    "لقد انتهى هذا العرض",
    "انتهت صلاحية هذا العرض",
    "انتهت فترة العرض",
    "العرض الترويجي انتهى",
    "لم يعد هذا العرض الترويجي متاح",
    "لم يعد هذا العرض متاحًا",
    "لم يعد هذا العرض متاحا",
)

# Markers indicating CAPTCHA or unavailable error page
_CAPTCHA_OR_ERROR_MARKERS = (
    "enter the characters you see below",
    "type the characters you see in this image",
    "validatecaptcha",
    "robot check",
    "sorry! something went wrong",
    "looking for something?",
    "عذراً، الصفحة التي تبحث عنها غير موجودة",
    "هل تبحث عن شيء ما؟",
)


class PromotionError(Exception):
    """Base error for promotion URL processing."""


class InvalidPromotionUrlError(PromotionError):
    """Raised when a promotion URL is malformed, missing a promotion ID, or not an Amazon URL."""


class PromotionNavigationError(PromotionError):
    """Raised when opening or rendering the promotion page fails (timeout, CAPTCHA, unavailable)."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        if retryable is None:
            self.retryable = (
                status_code in RETRYABLE_STATUS_CODES
                or "503" in message
                or "timeout" in message.lower()
                or "temporarily" in message.lower()
            )
        else:
            self.retryable = retryable


class PromotionScreenshotError(PromotionError):
    """Raised when capturing or verifying the promotion screenshot fails."""


class PromotionPublishError(PromotionError):
    """Raised when uploading the promotion photo to Telegram fails."""


def _validate_screenshot_file(screenshot_path: str) -> None:
    """
    Ensure the captured screenshot file exists, is non-empty, and is not a blank
    single-color image. Raises PromotionScreenshotError on invalid/blank files.
    """
    if not screenshot_path or not os.path.isfile(screenshot_path):
        raise PromotionScreenshotError(f"Promotion screenshot file does not exist: {screenshot_path}")

    size = os.path.getsize(screenshot_path)
    if size <= 0:
        raise PromotionScreenshotError(f"Promotion screenshot file is empty (0 bytes): {screenshot_path}")

    try:
        with open(screenshot_path, "rb") as f:
            header = f.read(8)
    except Exception as exc:
        raise PromotionScreenshotError(f"Cannot read screenshot file {screenshot_path}: {exc}") from exc

    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = header.startswith(b"\xff\xd8\xff")
    if is_png or is_jpeg:
        try:
            with Image.open(screenshot_path) as img:
                width, height = img.size
                if width <= 0 or height <= 0:
                    raise PromotionScreenshotError("Screenshot has invalid zero dimensions")
                extrema = img.convert("L").getextrema()
                if extrema is not None and extrema[0] == extrema[1]:
                    raise PromotionScreenshotError(
                        f"Captured promotion screenshot is completely blank (uniform pixel value={extrema[0]})"
                    )
        except PromotionScreenshotError:
            raise
        except Exception as exc:
            raise PromotionScreenshotError(f"Invalid image file at {screenshot_path}: {exc}") from exc


def _clean_raw_url(url: str) -> str:
    """Strip surrounding whitespace and trailing punctuation."""
    return (url or "").strip().rstrip(".,)>]")


def is_promotion_url(url: str) -> bool:
    """
    Detect whether `url` is an Amazon promotion-page URL whose path contains `/promotion/psp/`.
    Supports query strings and URL-encoded parameters.
    """
    if not url or not isinstance(url, str):
        return False

    cleaned = _clean_raw_url(url)
    if not cleaned.lower().startswith(("http://", "https://")):
        return False

    try:
        parts = urlsplit(cleaned)
        host = (parts.hostname or "").lower()
        if not _AMAZON_HOST_RE.search(host):
            return False

        raw_path = parts.path or ""
        decoded_path = unquote(raw_path)
        return bool(
            _PROMO_PATH_MARKER_RE.search(raw_path)
            or _PROMO_PATH_MARKER_RE.search(decoded_path)
        )
    except Exception:
        return False


def extract_promotion_id(url: str) -> str | None:
    """Extract the promotion ID from an Amazon `/promotion/psp/<ID>` URL."""
    if not is_promotion_url(url):
        return None

    cleaned = _clean_raw_url(url)
    try:
        parts = urlsplit(cleaned)
        for candidate_path in (parts.path or "", unquote(parts.path or "")):
            match = _PROMO_ID_RE.search(candidate_path)
            if match:
                promo_id = match.group(1).strip().upper()
                if promo_id:
                    return promo_id
    except Exception:
        pass
    return None


def normalize_promotion_url(url: str, domain: str = AMAZON_DOMAIN) -> str:
    """
    Normalize and validate an Amazon promotion URL.

    - Ensures scheme is https and host is a valid Amazon domain (normalizes bare amazon.eg to www.amazon.eg).
    - Validates that path contains `/promotion/psp/<PROMO_ID>`.
    - Preserves all query parameters (including URL-encoded parameters, `ref`, `redirectAsin`, `redirectMerchantId`, `source`, etc.).
    - Raises `InvalidPromotionUrlError` if the URL is invalid or missing a promotion ID.
    """
    if not url or not isinstance(url, str):
        raise InvalidPromotionUrlError("Promotion URL is empty")

    cleaned = _clean_raw_url(url)
    if not cleaned.lower().startswith(("http://", "https://")):
        raise InvalidPromotionUrlError(f"Invalid URL scheme in promotion URL: {url!r}")

    try:
        parts = urlsplit(cleaned)
    except Exception as exc:
        raise InvalidPromotionUrlError(f"Malformed promotion URL: {url!r}") from exc

    host = (parts.hostname or "").strip().lower()
    if not host or not _AMAZON_HOST_RE.search(host):
        raise InvalidPromotionUrlError(f"Not a valid Amazon domain: {host!r}")

    # Normalize bare amazon.eg -> www.amazon.eg
    if host == "amazon.eg":
        host = "www.amazon.eg"
    elif not host.startswith("www.") and host.startswith("amazon."):
        host = f"www.{host}"

    decoded_path = unquote(parts.path or "")
    if not _PROMO_PATH_MARKER_RE.search(decoded_path):
        raise InvalidPromotionUrlError(f"URL path does not contain /promotion/psp/: {url!r}")

    match = _PROMO_ID_RE.search(decoded_path)
    if not match:
        raise InvalidPromotionUrlError(f"Missing or invalid promotion ID in URL: {url!r}")

    promo_id = match.group(1).strip().upper()

    # Preserve optional language prefix if present, otherwise standard /promotion/psp/<promo_id>
    lang_match = re.match(r"^(/-[a-zA-Z_/]+)?/promotion/psp/", decoded_path, re.IGNORECASE)
    prefix = (lang_match.group(1) or "") if lang_match else ""
    normalized_path = f"{prefix}/promotion/psp/{promo_id}"

    # Parse and re-encode query parameters while preserving order and values
    raw_query = parts.query or ""
    if "%" in raw_query and "=" not in raw_query:
        # Handle fully URL-encoded query string if passed
        raw_query = unquote(raw_query)

    query_pairs = parse_qsl(raw_query, keep_blank_values=True)
    normalized_query = urlencode(query_pairs, doseq=True)

    return urlunsplit(("https", host, normalized_path, normalized_query, ""))


def is_expired_promotion_text(text: str | None) -> bool:
    """Return True if text explicitly indicates that the promotion has ended/expired."""
    if not text:
        return False
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    for marker in _EXPIRED_PROMOTION_MARKERS:
        if marker.lower() in normalized:
            return True
    return False


def clean_promotion_title(raw: str | None, *, strip_branding: bool = False) -> str | None:
    """
    Normalize whitespace, remove irrelevant UI text, and optionally remove generic Amazon branding.
    Returns None if no meaningful title remains.
    """
    if not raw or not isinstance(raw, str):
        return None

    # Replace zero-width chars and normalize whitespace
    text = re.sub(r"[\u200b\u200e\u200f\ufeff\xa0\r\n\t]+", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    if strip_branding:
        for pattern in _BRANDING_PATTERNS:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE).strip()

    for ui_pattern in _IRRELEVANT_UI_PATTERNS:
        text = re.sub(ui_pattern, " ", text, flags=re.IGNORECASE)

    # Clean up dangling separators or pipes
    text = re.sub(r"\s+", " ", text).strip(" |:-–—•.,")
    if not text or len(text) < 3:
        return None

    if text.lower() in _GENERIC_TITLES:
        return None

    return text


async def _safe_locator_text(locator, *, check_visible: bool = False) -> str | None:
    """Safely extract text from the first matching element of a Playwright locator."""
    try:
        count = await locator.count()
        if count <= 0:
            return None
        for idx in range(min(count, 5)):
            el = locator.nth(idx)
            if check_visible:
                try:
                    if not await el.is_visible():
                        continue
                except Exception:
                    pass
            try:
                txt = await el.text_content()
            except Exception:
                txt = None
            if not txt or not txt.strip():
                try:
                    txt = await el.inner_text()
                except Exception:
                    txt = None
            if txt and txt.strip():
                return txt.strip()
    except Exception:
        pass
    return None


async def _safe_meta_content(page, selector: str) -> str | None:
    """Safely extract `content` attribute from a meta tag selector."""
    try:
        locator = page.locator(selector)
        if await locator.count() > 0:
            val = await locator.first.get_attribute("content")
            if val and val.strip():
                return val.strip()
    except Exception:
        pass
    return None


async def detect_promotion_expired(page, extracted_title: str | None = None) -> bool:
    """
    Check if the rendered promotion page explicitly indicates that the promotion has ended.
    """
    if is_expired_promotion_text(extracted_title):
        return True

    expired_selectors = [
        "#promotionEnded",
        "#promoEnded",
        "[id*='promotionEnded']",
        "[class*='promotionEnded']",
        ".a-alert-heading",
        ".a-alert-content",
        "#promotionTitle",
        "#promotionSchedule",
        "h1",
        "h2",
        "body",
    ]
    for selector in expired_selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for idx in range(min(count, 5)):
                txt = await locator.nth(idx).text_content()
                if is_expired_promotion_text(txt):
                    logger.info("EXPIRED PROMOTION DETECTED via selector=%s text=%r", selector, (txt or "").strip()[:120])
                    return True
        except Exception:
            continue

    try:
        body_text = await page.inner_text("body")
        if is_expired_promotion_text(body_text):
            logger.info("EXPIRED PROMOTION DETECTED in page body")
            return True
    except Exception:
        pass

    return False


async def extract_promotion_title(page) -> tuple[str, str, bool]:
    """
    Extract the promotion title using the 5-level priority hierarchy:
    1. `#promotionTitle` text content.
    2. A meaningful visible page heading.
    3. The document title, after removing generic Amazon branding.
    4. Open Graph title or other relevant page metadata.
    5. A meaningful promotion description found in the rendered page.

    Returns:
        (title, source, title_missing)
        If the title cannot be identified, returns (FALLBACK_PROMOTION_TITLE, "fallback", True).
    """
    # Priority 1: #promotionTitle text content
    raw_p1 = await _safe_locator_text(page.locator("#promotionTitle"))
    cleaned_p1 = clean_promotion_title(raw_p1, strip_branding=False)
    if cleaned_p1 and not is_expired_promotion_text(cleaned_p1):
        logger.info("PROMOTION TITLE EXTRACTED priority=1 (#promotionTitle): %r", cleaned_p1)
        return cleaned_p1, "promotionTitle", False

    # Priority 2: Meaningful visible page heading
    heading_selectors = [
        "[id*='promotionTitle']",
        ".promotionTitle",
        "[data-csa-c-content-id*='promotion'] h1",
        "[data-csa-c-content-id*='promotion'] h2",
        ".psp-header h1",
        ".psp-header h2",
        "main h1",
        "#a-page h1",
        "h1",
        "main h2",
        "h2",
    ]
    for selector in heading_selectors:
        raw_h = await _safe_locator_text(page.locator(selector), check_visible=True)
        cleaned_h = clean_promotion_title(raw_h, strip_branding=True)
        if cleaned_h and not is_expired_promotion_text(cleaned_h):
            logger.info("PROMOTION TITLE EXTRACTED priority=2 (heading %s): %r", selector, cleaned_h)
            return cleaned_h, "heading", False

    # Priority 3: Document title after removing generic Amazon branding
    try:
        doc_title = await page.title()
    except Exception:
        doc_title = None
    cleaned_doc = clean_promotion_title(doc_title, strip_branding=True)
    if cleaned_doc and not is_expired_promotion_text(cleaned_doc):
        logger.info("PROMOTION TITLE EXTRACTED priority=3 (document_title): %r", cleaned_doc)
        return cleaned_doc, "document_title", False

    # Priority 4: Open Graph title or other relevant page metadata
    meta_selectors = [
        "meta[property='og:title']",
        "meta[name='og:title']",
        "meta[name='title']",
        "meta[property='twitter:title']",
        "meta[name='twitter:title']",
        "meta[property='og:description']",
        "meta[name='description']",
    ]
    for selector in meta_selectors:
        raw_meta = await _safe_meta_content(page, selector)
        cleaned_meta = clean_promotion_title(raw_meta, strip_branding=True)
        if cleaned_meta and not is_expired_promotion_text(cleaned_meta):
            logger.info("PROMOTION TITLE EXTRACTED priority=4 (metadata %s): %r", selector, cleaned_meta)
            return cleaned_meta, "metadata", False

    # Priority 5: Meaningful promotion description found in the rendered page
    desc_selectors = [
        "#promotionDescription",
        "#promotionDetails",
        ".promotionDescription",
        "#promotionSchedule",
        ".psp-subheader",
        "[id*='promotion'] .a-size-large",
        "[id*='promotion'] .a-size-medium",
        "[class*='psp'] .a-size-large",
        "[class*='psp'] .a-size-medium",
    ]
    for selector in desc_selectors:
        raw_desc = await _safe_locator_text(page.locator(selector), check_visible=True)
        cleaned_desc = clean_promotion_title(raw_desc, strip_branding=True)
        if cleaned_desc and not is_expired_promotion_text(cleaned_desc):
            logger.info("PROMOTION TITLE EXTRACTED priority=5 (description %s): %r", selector, cleaned_desc)
            return cleaned_desc, "description", False

    logger.warning("PROMOTION TITLE MISSING — using fallback title %r", FALLBACK_PROMOTION_TITLE)
    return FALLBACK_PROMOTION_TITLE, "fallback", True


async def _check_page_unavailable_or_captcha(page) -> None:
    """Raise PromotionNavigationError if page is CAPTCHA, robot check, or 404/error page."""
    current_url = (getattr(page, "url", "") or "").lower()
    if "validatecaptcha" in current_url or "/errors/" in current_url:
        raise PromotionNavigationError(f"CAPTCHA or error page encountered at {page.url}")

    try:
        html_or_text = await page.inner_text("body")
    except Exception:
        html_or_text = ""

    lower_text = (html_or_text or "").lower()
    for marker in _CAPTCHA_OR_ERROR_MARKERS:
        if marker in lower_text:
            raise PromotionNavigationError(f"Unavailable or CAPTCHA page detected ({marker})")


async def _bypass_continue_shopping_if_present(page, promo_id: str) -> None:
    """Click through Amazon's 'Continue shopping' / 'متابعة التسوق' interstitial if shown."""
    try:
        continue_button = page.locator(
            "button.a-button-text[alt*='متابعة التسوق'], button:has-text('متابعة التسوق'), button:has-text('Continue shopping')"
        )
        if await continue_button.count() > 0:
            await continue_button.first.click(timeout=3000)
            logger.info("CONTINUE SHOPPING INTERSTITIAL BYPASSED promo_id=%s", promo_id)
            await page.wait_for_timeout(1500)
    except Exception:
        pass


async def _trigger_lazy_content(page) -> None:
    """Scroll gently to trigger lazy-loaded promotion items and return to top."""
    try:
        await page.evaluate("window.scrollBy(0, 600)")
        await page.wait_for_timeout(400)
        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(300)
    except Exception:
        pass


async def hide_irrelevant_promotion_elements(page) -> bool:
    """
    Inject CSS via `page.add_style_tag()` to hide Amazon's header, navigation,
    footer, cookie notices, and unrelated banners before taking the screenshot.
    Does not remove elements from the DOM or modify product links.
    """
    add_style = getattr(page, "add_style_tag", None)
    if callable(add_style):
        try:
            await add_style(content=PROMOTION_SCREENSHOT_CSS)
            try:
                await page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass
            await page.wait_for_timeout(150)
            logger.info("PROMOTION SCREENSHOT CSS INJECTED (header/footer hidden)")
            return True
        except Exception as exc:
            logger.warning("Failed to inject promotion screenshot CSS via add_style_tag: %s", exc)
    return False


def _is_valid_box(box: dict[str, Any] | None, *, min_width: float = 200.0, min_height: float = 20.0) -> bool:
    """Return True if a Playwright bounding box dict has positive, usable dimensions."""
    if not isinstance(box, dict):
        return False
    try:
        w = float(box.get("width", 0))
        h = float(box.get("height", 0))
        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
    except (TypeError, ValueError):
        return False
    return w >= min_width and h >= min_height and x >= -50 and y >= -50


async def _get_element_box(el) -> dict[str, float] | None:
    """Safely obtain bounding_box() from a Playwright element/locator if visible."""
    try:
        if hasattr(el, "is_visible") and not await el.is_visible():
            return None
    except Exception:
        pass
    bbox_fn = getattr(el, "bounding_box", None)
    if not callable(bbox_fn):
        return None
    try:
        raw_box = await bbox_fn()
        if isinstance(raw_box, dict) and "width" in raw_box and "height" in raw_box:
            return {
                "x": float(raw_box.get("x", 0.0)),
                "y": float(raw_box.get("y", 0.0)),
                "width": float(raw_box.get("width", 0.0)),
                "height": float(raw_box.get("height", 0.0)),
            }
    except Exception:
        pass
    return None


async def _find_promotion_container_locator(page) -> tuple[Any | None, dict[str, float] | None, str | None]:
    """
    Identify a single promotion container element if it encompasses the promotion content
    (title, filters, and product listings).

    Returns:
        (element, bounding_box, selector) or (None, None, None)
    """
    title_box: dict[str, float] | None = None
    try:
        title_loc = page.locator("#promotionTitle")
        if await title_loc.count() > 0:
            title_box = await _get_element_box(title_loc.first)
    except Exception:
        title_box = None

    for selector in PROMOTION_CONTAINER_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            if count <= 0:
                continue
            el = locator.first
            box = await _get_element_box(el)
            if not _is_valid_box(box, min_width=300.0, min_height=150.0):
                continue

            # If #promotionTitle has a known bounding box, verify this container encompasses it
            if title_box is not None:
                c_top = box["y"]
                c_bottom = box["y"] + box["height"]
                t_top = title_box["y"]
                if c_top > t_top + 25 or c_bottom < t_top + title_box["height"] - 10:
                    continue

            return el, box, selector
        except Exception:
            continue

    return None, None, None


async def _calculate_promotion_bounding_box(
    page,
    container_box: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """
    Calculate a safe bounding box around the promotion content (title, date range, filters,
    and product listings), excluding headers, footers, and excessive whitespace.
    """
    boxes: list[dict[str, float]] = []
    if _is_valid_box(container_box, min_width=300.0, min_height=120.0):
        boxes.append(container_box)

    for selector in PROMOTION_SECTION_SELECTORS:
        try:
            locator = page.locator(selector)
            count = await locator.count()
            for idx in range(min(count, 6)):
                el = locator.nth(idx)
                box = await _get_element_box(el)
                if _is_valid_box(box, min_width=180.0, min_height=20.0):
                    boxes.append(box)
        except Exception:
            continue

    if not boxes:
        return None

    min_x = min(b["x"] for b in boxes)
    min_y = min(b["y"] for b in boxes)
    max_x = max(b["x"] + b["width"] for b in boxes)
    max_y = max(b["y"] + b["height"] for b in boxes)

    pad = 12.0
    clip_x = max(0.0, min_x - pad)
    clip_y = max(0.0, min_y - pad)
    clip_w = min(float(PROMOTION_VIEWPORT_WIDTH) - clip_x, (max_x - min_x) + 2 * pad)
    raw_h = (max_y - min_y) + 2 * pad

    if clip_w < 200.0 or raw_h < 80.0:
        return None

    # Keep full product listing when height is reasonable; cap when page is very long
    # without cutting off the promotion header or first product row.
    if raw_h > MAX_FULL_PAGE_HEIGHT:
        clip_h = float(max(MIN_PROMOTION_CLIP_HEIGHT, MAX_FULL_PAGE_HEIGHT))
    else:
        clip_h = raw_h

    return {
        "x": round(clip_x, 2),
        "y": round(clip_y, 2),
        "width": round(clip_w, 2),
        "height": round(clip_h, 2),
    }


async def _capture_promotion_screenshot(page, screenshot_path: str) -> str:
    """
    Capture a screenshot focused on the promotion content:
    1. Prefer a locator screenshot when the promotion content is contained in one suitable
       element of reasonable height (`<= MAX_FULL_PAGE_HEIGHT`).
    2. Otherwise, calculate a safe bounding box around the promotion title, filters, and
       product listings (capping height if very long) and capture that region via `clip`.
    3. Fall back to a page screenshot (full-page when short, viewport when very long) if
       container selectors or bounding boxes are unavailable.
    Verifies file existence and non-zero size on disk.
    """
    await hide_irrelevant_promotion_elements(page)

    container_el, container_box, container_sel = await _find_promotion_container_locator(page)

    try:
        if (
            container_el is not None
            and container_box is not None
            and container_box["height"] <= MAX_FULL_PAGE_HEIGHT
            and callable(getattr(container_el, "screenshot", None))
        ):
            logger.info(
                "PROMOTION SCREENSHOT CAPTURE mode=locator selector=%s path=%s box=%s",
                container_sel,
                screenshot_path,
                container_box,
            )
            await container_el.screenshot(path=screenshot_path)
        else:
            clip_box = await _calculate_promotion_bounding_box(page, container_box=container_box)
            if clip_box is not None:
                logger.info(
                    "PROMOTION SCREENSHOT CAPTURE mode=clip path=%s clip=%s",
                    screenshot_path,
                    clip_box,
                )
                await page.screenshot(path=screenshot_path, clip=clip_box)
            else:
                try:
                    page_height = await page.evaluate(
                        "() => Math.max("
                        "document.body ? document.body.scrollHeight : 0, "
                        "document.documentElement ? document.documentElement.scrollHeight : 0"
                        ")"
                    )
                except Exception:
                    page_height = 0

                use_full_page = (
                    isinstance(page_height, (int, float))
                    and 0 < page_height <= MAX_FULL_PAGE_HEIGHT
                )
                logger.info(
                    "PROMOTION SCREENSHOT CAPTURE mode=page_fallback path=%s page_height=%s full_page=%s",
                    screenshot_path,
                    page_height,
                    use_full_page,
                )
                await page.screenshot(path=screenshot_path, full_page=use_full_page)
    except Exception as exc:
        cleanup_files([screenshot_path])
        raise PromotionScreenshotError(f"Failed to capture promotion screenshot: {exc}") from exc

    try:
        _validate_screenshot_file(screenshot_path)
    except Exception:
        cleanup_files([screenshot_path])
        raise

    return screenshot_path


async def scrape_promotion_page(
    browser_mgr: BrowserManager,
    normalized_url: str,
    scrape_key: str,
) -> dict[str, Any]:
    """
    Open the promotion page using the shared BrowserManager, wait for rendering,
    extract promotion title and expired status, and capture a screenshot.
    """
    if browser_mgr is None or not getattr(browser_mgr, "_browser", None):
        raise PromotionNavigationError("Shared browser is not started")

    promo_id = extract_promotion_id(normalized_url) or "UNKNOWN"
    browser = browser_mgr._browser

    context = await browser.new_context(
        viewport={"width": PROMOTION_VIEWPORT_WIDTH, "height": PROMOTION_VIEWPORT_HEIGHT},
        user_agent=USER_AGENT,
        locale="ar-EG",
        timezone_id="Africa/Cairo",
        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},
    )
    page = await context.new_page()
    screenshot_path = f"{scrape_key}.png"

    try:
        # Navigate with bounded retry, exponential backoff, and jitter
        last_nav_err: Exception | None = None
        last_status: int | None = None
        status_display: str = "unknown"
        attempts_made: int = 0

        for attempt in range(1, MAX_NAVIGATION_RETRIES + 1):
            attempts_made = attempt
            try:
                response = await page.goto(
                    normalized_url,
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                status_code = getattr(response, "status", 200) if response is not None else 200
                last_status = status_code
                status_display = f"HTTP_{status_code}"

                if status_code in RETRYABLE_STATUS_CODES:
                    err_msg = f"HTTP {status_code} when loading promotion page {normalized_url}"
                    if attempt < MAX_NAVIGATION_RETRIES:
                        backoff = min(
                            MAX_BACKOFF_SECONDS,
                            BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                        ) + random.uniform(0.1, BACKOFF_JITTER_SECONDS)
                        logger.warning(
                            "NAVIGATION RETRY [promotion_handler]:\n"
                            "  handler=promotion_handler\n"
                            "  url=%s\n"
                            "  attempt=%d/%d\n"
                            "  status=%s\n"
                            "  retryable=True\n"
                            "  backoff=%.2fs",
                            normalized_url,
                            attempt,
                            MAX_NAVIGATION_RETRIES,
                            status_display,
                            backoff,
                        )
                        await _nav_sleep(backoff)
                        continue
                    else:
                        last_nav_err = PromotionNavigationError(
                            err_msg,
                            status_code=status_code,
                            retryable=True,
                        )
                        break

                if 400 <= status_code < 500 and status_code not in RETRYABLE_STATUS_CODES:
                    # Non-retryable client error (e.g. 404 Not Found)
                    last_nav_err = PromotionNavigationError(
                        f"HTTP {status_code} when loading promotion page {normalized_url}",
                        status_code=status_code,
                        retryable=False,
                    )
                    break

                # Success (2xx / 3xx)
                last_nav_err = None
                logger.info(
                    "NAVIGATION SUCCESS [promotion_handler]:\n"
                    "  handler=promotion_handler\n"
                    "  url=%s\n"
                    "  attempt=%d/%d\n"
                    "  status=%d",
                    normalized_url,
                    attempt,
                    MAX_NAVIGATION_RETRIES,
                    status_code,
                )
                break

            except PromotionNavigationError as p_err:
                last_nav_err = p_err
                break
            except Exception as exc:
                last_nav_err = exc
                status_display = type(exc).__name__
                if attempt < MAX_NAVIGATION_RETRIES:
                    backoff = min(
                        MAX_BACKOFF_SECONDS,
                        BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
                    ) + random.uniform(0.1, BACKOFF_JITTER_SECONDS)
                    logger.warning(
                        "NAVIGATION RETRY [promotion_handler]:\n"
                        "  handler=promotion_handler\n"
                        "  url=%s\n"
                        "  attempt=%d/%d\n"
                        "  status=%s\n"
                        "  retryable=True\n"
                        "  backoff=%.2fs\n"
                        "  error=%s",
                        normalized_url,
                        attempt,
                        MAX_NAVIGATION_RETRIES,
                        status_display,
                        backoff,
                        exc,
                    )
                    await _nav_sleep(backoff)
                    continue
                else:
                    break

        if last_nav_err is not None:
            is_retryable = getattr(last_nav_err, "retryable", True)
            final_reason = str(last_nav_err)
            logger.error(
                "NAVIGATION FAILED [promotion_handler]:\n"
                "  handler=promotion_handler\n"
                "  url=%s\n"
                "  retry_count=%d\n"
                "  navigation_status=%s\n"
                "  retryable=%s\n"
                "  final_failure_reason=%s",
                normalized_url,
                attempts_made,
                status_display,
                is_retryable,
                final_reason,
            )
            if isinstance(last_nav_err, PromotionNavigationError):
                raise last_nav_err
            raise PromotionNavigationError(
                f"Navigation failed for promotion URL {normalized_url}: {last_nav_err}",
                status_code=last_status,
                retryable=is_retryable,
            ) from last_nav_err

        await _bypass_continue_shopping_if_present(page, promo_id)
        await _check_page_unavailable_or_captcha(page)

        # Wait for promotion title or main content container to attach
        try:
            await page.wait_for_selector(
                "#promotionTitle, [id*='promotion'], main, #a-page",
                 state="attached",
                timeout=8000,
            )
        except Exception:
            logger.debug("Promotion primary selector wait timed out for promo_id=%s; continuing", promo_id)

        await page.wait_for_timeout(1500)
        await _trigger_lazy_content(page)

        is_expired = await detect_promotion_expired(page)
        title, title_source, title_missing = await extract_promotion_title(page)

        verified_screenshot = await _capture_promotion_screenshot(page, screenshot_path)

        return {
            "promotion_id": promo_id,
            "normalized_url": normalized_url,
            "title": title,
            "title_source": title_source,
            "title_missing": title_missing,
            "is_expired": is_expired,
            "screenshot": verified_screenshot,
        }
    except Exception:
        if os.path.exists(screenshot_path):
            cleanup_files([screenshot_path])
        raise
    finally:
        await context.close()


async def shorten_promotion_url(
    normalized_url: str,
    db: Any = None,
) -> tuple[str, bool]:
    """
    Apply affiliate tag while preserving all promotion query parameters,
    and shorten via Amazon SiteStripe shortener.
    Falls back to the valid affiliate-tagged promotion URL if shortening fails.

    Returns:
        (final_display_url, was_shortened)
    """
    tagged_url = apply_affiliate_tag(normalized_url)
    try:
        short_url = await shorten_amazon_url(tagged_url, db)
        if short_url and isinstance(short_url, str) and short_url.strip():
            return short_url.strip(), True
    except Exception as exc:
        logger.warning(
            "PROMOTION SHORTENER ERROR (falling back to valid promotion URL): %s",
            type(exc).__name__,
        )

    return tagged_url, False


def build_promotion_caption(
    title: str,
    display_url: str,
    *,
    is_expired: bool = False,
    title_missing: bool = False,
) -> str:
    """
    Build the Telegram caption for an Amazon promotion page.
    Includes:
    - Extracted promotion name (or fallback `عرض ترويجي من أمازون`).
    - Clear indication if the promotion has expired or its title could not be extracted.
    - Shortened (or fallback valid) promotion URL.
    """
    clean_title = (title or "").strip() or FALLBACK_PROMOTION_TITLE

    lines: list[str] = []
    if is_expired:
        lines.append("⚠️ انتهى هذا العرض الترويجي (العرض غير نشط حالياً)")
        lines.append("")
        lines.append(f"🏷️ {clean_title}")
        if title_missing:
            lines.append("⚠️ تعذر استخراج عنوان العرض الترويجي تلقائياً")
        lines.append("")
        lines.append("🔗 رابط صفحة العرض:")
        lines.append(display_url)
    else:
        lines.append("🎉 عرض ترويجي من أمازون")
        lines.append("")
        if title_missing:
            lines.append(f"🏷️ {FALLBACK_PROMOTION_TITLE}")
            lines.append("⚠️ تعذر استخراج عنوان العرض الترويجي تلقائياً")
        else:
            lines.append(f"🏷️ {clean_title}")
        lines.append("")
        lines.append("🔗 لينك العرض:")
        lines.append(display_url)

    return "\n".join(lines)


async def process_promotion_url(
    browser: BrowserManager,
    db: Any,
    url: str,
    *,
    scrape_key: str | None = None,
    domain: str = AMAZON_DOMAIN,
) -> dict[str, Any]:
    """
    End-to-end promotion URL processing flow:
    1. Normalize and validate the Amazon.eg promotion URL.
    2. Open the page using the shared Playwright BrowserManager.
    3. Wait for the page to render.
    4. Extract the promotion name (and expired status).
    5. Capture a screenshot.
    6. Shorten the promotion URL using the existing Amazon shortener (with fallback).
    7. Return structured data including screenshot, extracted title, shortened URL, and caption.
    """
    normalized_url = normalize_promotion_url(url, domain=domain)
    promo_id = extract_promotion_id(normalized_url) or "PROMO"

    if not scrape_key:
        scrape_key = f"promo_{promo_id}_{int(time.time() * 1000)}"

    scraped = await scrape_promotion_page(browser, normalized_url, scrape_key)
    screenshot_path = scraped["screenshot"]

    try:
        display_url, was_shortened = await shorten_promotion_url(normalized_url, db)
        caption = build_promotion_caption(
            scraped["title"],
            display_url,
            is_expired=scraped["is_expired"],
            title_missing=scraped["title_missing"],
        )
        return {
            "promotion_id": promo_id,
            "original_url": url,
            "normalized_url": normalized_url,
            "display_url": display_url,
            "short_url": display_url,
            "shortened": was_shortened,
            "title": scraped["title"],
            "title_source": scraped["title_source"],
            "title_missing": scraped["title_missing"],
            "is_expired": scraped["is_expired"],
            "screenshot": screenshot_path,
            "caption": caption,
        }
    except Exception:
        cleanup_files([screenshot_path])
        raise


async def publish_promotion_to_telegram(
    application: Any,
    db: Any,
    promo_data: dict[str, Any],
    *,
    cleanup_after: bool = True,
) -> bool:
    """
    Publish a processed promotion to enabled Telegram destination channels as a photo.
    Keeps temporary image files available until upload completes, then cleans them up.
    """
    screenshot_path = promo_data.get("screenshot")
    if not screenshot_path or not os.path.isfile(screenshot_path):
        raise PromotionScreenshotError("Cannot publish promotion: screenshot file does not exist")

    temp_files: list[str] = [screenshot_path] if cleanup_after else []
    try:
        upload_image = to_jpeg_for_telegram(screenshot_path)
        if upload_image != screenshot_path:
            temp_files.append(upload_image)

        display_url = promo_data.get("display_url") or promo_data.get("short_url") or promo_data.get("normalized_url", "")
        products = [{"title": promo_data["title"], "url": display_url}]
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

        destinations = db.get_enabled_destinations()
        if not destinations:
            logger.error("No enabled destinations configured for promotion publish")
            return False

        result = await publish_to_destinations(
            application.bot,
            destinations,
            upload_image,
            promo_data["caption"],
            reply_markup=inline_keyboard if inline_keyboard.inline_keyboard else None,
            products=products,
            publish_type="PROMOTION",
        )
        result.log_summary()
        if result.successful <= 0:
            raise PromotionPublishError("Failed to upload promotion photo to any Telegram destination")
        return True
    finally:
        if temp_files:
            cleanup_files(temp_files)
