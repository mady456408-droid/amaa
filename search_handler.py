"""Amazon search-results URL detection, affiliate tag normalization, screenshot capture, and Telegram publishing."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from PIL import Image

from affiliate_tag import get_default_affiliate_tag
from amazon_scraper import USER_AGENT, BrowserManager
from config import AMAZON_DOMAIN, LAST_PUBLISHED_LOOKBACK
from file_cleanup import cleanup_files
from inline_buttons import build_inline_keyboard
from multi_publisher import publish_to_destinations
from upload_prep import to_jpeg_for_telegram


async def send_approval_request(*args: Any, **kwargs: Any) -> Any:
    """Lazy wrapper around duplicate_moderation.send_approval_request to avoid circular imports."""
    from duplicate_moderation import send_approval_request as _send_approval_request

    return await _send_approval_request(*args, **kwargs)

logger = logging.getLogger(__name__)

SEARCH_KEYWORD_VALUE = "LoqtaBGD"
SEARCH_AFFILIATE_TAG = get_default_affiliate_tag()
FALLBACK_SEARCH_TITLE = "عروض ونتائج بحث أمازون"

SEARCH_VIEWPORT_WIDTH = 1366
SEARCH_VIEWPORT_HEIGHT = 900
MAX_FULL_PAGE_HEIGHT = 4500
MAX_CAPTURE_HEIGHT = 3600
MIN_CONTAINER_WIDTH = 200
MIN_CONTAINER_HEIGHT = 120
MAX_INLINE_BUTTON_URL_BYTES = 512

_ACCEPT_LANGUAGE = "ar-EG,ar;q=0.9,en-US;q=0.8,en;q=0.7"

# Injected CSS to hide Amazon navigation headers, promotional bars, cookie notices, and footers
# while keeping product cards, search results, prices, ratings, and deal badges visible.
SEARCH_SCREENSHOT_CSS = """
#navbar,
#navbar-main,
#nav-main,
#nav-belt,
#nav-subnav,
#nav-progressive-subnav,
#skiplink,
header#navbar-main,
header.nav-opt-sprite,
#nav-upnav,
#gw-desktop-herotator,
#navFooter,
#footer,
footer,
#rhf,
.rhf-frame,
#sp-cc,
#sp-cc-Accept,
[id^="sp-cc"],
.sp-cc-container,
#cookie-banner,
.a-cookie-consent-banner,
#nav-top {
    display: none !important;
    visibility: hidden !important;
    height: 0 !important;
    min-height: 0 !important;
    max-height: 0 !important;
    overflow: hidden !important;
    margin: 0 !important;
    padding: 0 !important;
    border: 0 !important;
}

html, body, #a-page, #search {
    margin-top: 0 !important;
    padding-top: 0 !important;
    margin-bottom: 0 !important;
    padding-bottom: 0 !important;
}
"""

SEARCH_CONTAINER_SELECTORS: tuple[str, ...] = (
    "#search .s-main-slot.s-result-list",
    ".s-main-slot.s-result-list",
    "#search .s-main-slot",
    ".s-main-slot",
    "#search .s-desktop-width-max",
    "#search",
)

SEARCH_PRODUCT_CARD_SELECTORS: tuple[str, ...] = (
    '[data-component-type="s-search-result"][data-asin]:not([data-asin=""])',
    '[data-component-type="s-search-result"]',
    '.s-main-slot .s-result-item[data-asin]:not([data-asin=""])',
    ".s-result-item[data-asin]",
    ".s-result-item",
)

SEARCH_HEADER_SELECTORS: tuple[str, ...] = (
    '[data-component-type="s-result-info-bar"]',
    ".s-breadcrumb",
    ".s-desktop-toolbar",
)

_EMPTY_RESULTS_PHRASES: tuple[str, ...] = (
    "لا توجد نتائج لـ",
    "لا توجد نتائج",
    "لم يتم العثور على نتائج",
    "0 نتائج لـ",
    "no results for",
    "0 results for",
    "did not match any products",
)

# Regex matching `/s`, `/s/`, `/s/ref=...` with optional `/ar`, `/en`, `/-/ar`, `/-/en` language prefix
_SEARCH_PATH_RE = re.compile(
    r"^(?:/-(?:/)?(?:ar|en)|/(?:ar|en))?/s(?:/ref=[^/?#]*)?/?$",
    re.IGNORECASE,
)

# Regex matching ASIN-list queries (e.g. `B08N5WRWNW|B09G9FPHY6` or `B08N5WRWNW B09G9FPHY6`)
_ASIN_LIST_QUERY_RE = re.compile(
    r"^(?:[A-Z0-9]{10}[\s|,;+]*)+$",
    re.IGNORECASE,
)

# Tracking/ephemeral parameters excluded when computing canonical duplicate-check hash
_EPHEMERAL_SEARCH_PARAMS = frozenset({
    "tag",
    "ascsubtag",
    "linkcode",
    "linkid",
    "qid",
    "sprefix",
    "crid",
    "ref",
    "ref_",
    "th",
    "psc",
})


class SearchError(Exception):
    """Base exception for Amazon search-results handling errors."""


class InvalidSearchUrlError(SearchError):
    """Raised when a URL is not a valid Amazon search-results URL."""


class SearchNavigationError(SearchError):
    """Raised when the search page cannot be loaded (timeout, HTTP error, CAPTCHA, etc.)."""


class SearchEmptyResultsError(SearchError):
    """Raised when the search page contains no product results."""


class SearchScreenshotError(SearchError):
    """Raised when capturing a screenshot of the search page fails or produces a blank image."""


class SearchPublishError(SearchError):
    """Raised when publishing the search post to Telegram fails."""


def _is_amazon_host(hostname: str) -> bool:
    """Return True if hostname belongs to an Amazon storefront domain (not shorteners)."""
    host = (hostname or "").lower().strip().rstrip(".")
    if ":" in host:
        host = host.split(":", 1)[0]
    if not host:
        return False
    if host in ("amzn.to", "amzn.eu", "a.co"):
        return False
    return host == "amazon.eg" or host.endswith(".amazon.eg") or "amazon." in host


def _has_product_or_promotion_path(unquoted_path: str) -> bool:
    """Return True if the path is a product detail page or promotion page rather than search."""
    lower = unquoted_path.lower()
    if "/promotion/psp/" in lower:
        return True
    if re.search(r"/(?:dp|gp/product|gp/aw/d|d)/[a-z0-9]{10}(?:[/?#]|$)", lower):
        return True
    return False


def is_search_url(url: str) -> bool:
    """
    Return True if `url` is an Amazon search-results URL (e.g. `https://www.amazon.eg/s?...`).
    Distinguishes search-results pages from individual product pages and promotion pages.
    Supports query strings, URL-encoded parameters, and optional language prefixes (`/-/ar/s`, `/-/en/s`).
    """
    if not url or not isinstance(url, str):
        return False
    raw = url.strip()
    if not raw.startswith(("http://", "https://")):
        return False
    try:
        parsed = urlsplit(raw)
    except Exception:
        return False

    if parsed.scheme.lower() not in ("http", "https"):
        return False
    if not _is_amazon_host(parsed.netloc):
        return False

    unquoted_path = unquote(parsed.path or "")
    if _has_product_or_promotion_path(unquoted_path):
        return False

    if not _SEARCH_PATH_RE.match(unquoted_path):
        return False

    # Require either query parameters or a `/s/ref=...` path segment
    has_query = bool((parsed.query or "").strip())
    has_ref_in_path = "/ref=" in unquoted_path.lower()
    return has_query or has_ref_in_path


def replace_search_affiliate_tag(
    url: str,
    affiliate_tag: str | None = None,
    *,
    keyword: str = SEARCH_KEYWORD_VALUE,
    db: Any = None,
) -> str:
    """
    Parse `url` using `urllib.parse` (`urlsplit`, `parse_qsl`, `urlencode`, `urlunsplit`):
    1. Replace the value of the exact `k` query parameter with `LoqtaBGD` (or add `k=LoqtaBGD` if missing).
    2. Replace the value of the exact `tag` query parameter with the bot's configured default
       Amazon Associates affiliate tag (or add `tag=<default_tag>` if missing).
    3. Preserve all other query parameters (including `s`, `hidden-keywords`, `rh`, `i`,
       `language`, `_encoding`, and encoded ASIN lists) in their original order and values.
    4. Modify only the exact `k` and `tag` parameters without altering unrelated parameters.
    """
    if not url or not isinstance(url, str):
        raise InvalidSearchUrlError("Empty or non-string search URL")

    raw = url.strip()
    try:
        parsed = urlsplit(raw)
    except Exception as exc:
        raise InvalidSearchUrlError(f"Malformed URL: {url!r}") from exc

    if not parsed.scheme or not parsed.netloc:
        raise InvalidSearchUrlError(f"Missing scheme or host in URL: {url!r}")

    resolved_tag = (
        affiliate_tag
        if affiliate_tag is not None
        else get_default_affiliate_tag(db)
    )

    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    updated_items: list[tuple[str, str]] = []
    k_replaced = False
    tag_replaced = False

    for key, value in query_items:
        if key == "k":
            if not k_replaced:
                updated_items.append(("k", keyword))
                k_replaced = True
            # Skip any duplicate `k` parameters
        elif key == "tag":
            if not tag_replaced:
                updated_items.append(("tag", resolved_tag))
                tag_replaced = True
            # Skip any duplicate `tag` parameters
        else:
            updated_items.append((key, value))

    if not k_replaced:
        updated_items.insert(0, ("k", keyword))

    if not tag_replaced:
        updated_items.append(("tag", resolved_tag))

    new_query = urlencode(updated_items, doseq=True)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, new_query, ""))


def normalize_search_url(
    url: str,
    domain: str = AMAZON_DOMAIN,
    affiliate_tag: str | None = None,
    *,
    keyword: str = SEARCH_KEYWORD_VALUE,
    db: Any = None,
) -> str:
    """
    Validate and normalize an Amazon search-results URL:
    - Verifies it is a valid Amazon `/s` search URL.
    - Normalizes scheme to `https` and host to `www.amazon.eg` (or `domain` for `.eg` hosts).
    - Replaces or adds `k=LoqtaBGD` and `tag=<default_affiliate_tag>` using `replace_search_affiliate_tag`.
    - Preserves all other search parameters, filters, sorting, hidden keywords, and ASIN lists.
    """
    if not is_search_url(url):
        raise InvalidSearchUrlError(f"Not a valid Amazon search URL: {url!r}")

    parsed = urlsplit(url.strip())
    host = parsed.netloc.strip()
    host_no_port = host.split(":", 1)[0].lower().rstrip(".")
    if host_no_port == "amazon.eg" or host_no_port.endswith(".amazon.eg"):
        normalized_host = domain
    elif not host_no_port.startswith("www.") and host_no_port.startswith("amazon."):
        normalized_host = f"www.{host_no_port}"
    else:
        normalized_host = host_no_port

    rebuilt = urlunsplit(("https", normalized_host, parsed.path, parsed.query, ""))
    return replace_search_affiliate_tag(
        rebuilt,
        affiliate_tag=affiliate_tag,
        keyword=keyword,
        db=db,
    )


def compute_search_key(url: str) -> str:
    """
    Compute a deterministic identifier `SEARCH:<12-char-hex>` for duplicate detection
    based on the canonical search path and non-ephemeral query parameters.
    """
    try:
        parsed = urlsplit(url.strip())
        path = re.sub(r"^/(?:-(?:/)?)?(?:ar|en)(?=/s)", "", unquote(parsed.path or ""), flags=re.I)
        path = re.sub(r"/ref=[^/?#]*", "", path, flags=re.I).rstrip("/") or "/s"
        items = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.lower() not in _EPHEMERAL_SEARCH_PARAMS
        ]
        items.sort(key=lambda kv: (kv[0].lower(), kv[1]))
        canonical = f"{path.lower()}?{urlencode(items, doseq=True)}"
    except Exception:
        canonical = (url or "").strip()

    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12].upper()
    return f"SEARCH:{digest}"


def _clean_search_title_candidate(raw: str | None) -> str:
    """Normalize whitespace and strip generic Amazon branding from a title candidate."""
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"^(?:amazon\.eg|amazon|أمازون\s*مصر|أمازون|امازون)\s*[:\-–—|]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        r"\s*[:\-–—|]\s*(?:amazon\.eg|amazon|أمازون\s*مصر|أمازون|امازون).*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = cleaned.strip("\"'«»“”")

    # Do not use raw ASIN lists as a human-facing title
    if _ASIN_LIST_QUERY_RE.match(cleaned):
        return ""
    if len(cleaned) < 2:
        return ""
    if cleaned.lower() in {"amazon", "amazon.eg", "أمازون", "أمازون مصر", "امازون"}:
        return ""
    return cleaned


async def _bypass_continue_shopping_if_present(page) -> None:
    """Click Amazon Egypt's 'Continue shopping / متابعة التسوق' interstitial button if present."""
    try:
        continue_selectors = [
            "button:has-text('متابعة التسوق')",
            "input[value*='متابعة التسوق']",
            "a:has-text('متابعة التسوق')",
            "button:has-text('Continue shopping')",
            "input[value*='Continue shopping']",
            "a:has-text('Continue shopping')",
            "span.a-button-inner:has-text('متابعة التسوق')",
            "span.a-button-inner:has-text('Continue shopping')",
        ]
        for sel in continue_selectors:
            btn = await page.query_selector(sel)
            if btn and await btn.is_visible():
                await btn.click()
                await page.wait_for_load_state("domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1000)
                break
    except Exception as exc:
        logger.debug("Search continue-shopping check skipped: %s", exc)


async def _check_page_unavailable_or_captcha(page) -> None:
    """Raise SearchNavigationError if page is a CAPTCHA challenge or error page."""
    current_url = (getattr(page, "url", "") or "").lower()
    if "validatecaptcha" in current_url or "opfcaptcha" in current_url:
        raise SearchNavigationError("CAPTCHA challenge encountered on Amazon search page")

    try:
        captcha_form = await page.query_selector(
            "form[action*='validateCaptcha'], #captchacharacters"
        )
        if captcha_form:
            raise SearchNavigationError("CAPTCHA form detected on Amazon search page")
    except SearchNavigationError:
        raise
    except Exception:
        pass

    try:
        title = (await page.title() or "").strip()
    except Exception:
        title = ""

    title_lower = title.lower()
    if "404 - document not found" in title_lower or "page not found" in title_lower:
        raise SearchNavigationError(f"Amazon search page unavailable (title={title!r})")
    if "🐕" in title or "dogs of amazon" in title_lower:
        raise SearchNavigationError(f"Amazon error dog page encountered (title={title!r})")


async def count_visible_search_results(page) -> int:
    """Count visible search-result product cards on the rendered page."""
    for sel in SEARCH_PRODUCT_CARD_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
        except Exception:
            elements = []
        if not elements:
            continue

        visible_count = 0
        for el in elements:
            try:
                if await el.is_visible():
                    box = await el.bounding_box()
                    if box and box.get("width", 0) >= 60 and box.get("height", 0) >= 60:
                        visible_count += 1
            except Exception:
                continue
        if visible_count > 0:
            return visible_count

    return 0


async def _check_empty_search_results(page) -> int:
    """
    Verify that the search page has rendered at least one visible product card
    and does not display an empty-results warning.
    Returns the number of visible product cards.
    Raises SearchEmptyResultsError if no results are found.
    """
    visible_count = await count_visible_search_results(page)

    try:
        body_text = await page.inner_text("body")
    except Exception:
        body_text = ""

    body_lower = (body_text or "").lower()
    has_empty_phrase = any(phrase in body_lower for phrase in _EMPTY_RESULTS_PHRASES)

    if visible_count == 0:
        if has_empty_phrase:
            raise SearchEmptyResultsError("Amazon search page returned 0 results (empty results message detected)")
        raise SearchEmptyResultsError("No visible product cards found on Amazon search page")

    return visible_count


async def extract_search_title(page, affiliate_url: str = "") -> str:
    """
    Extract a descriptive title for the search results page:
    1. Search result info bar bold keyword/category (`.a-color-state.a-text-bold`).
    2. Visible page heading (`h1`).
    3. URL `k` query parameter (if not a raw ASIN list).
    4. Document `<title>` (cleaned of Amazon branding, if not a raw ASIN list).
    5. Fallback `عروض ونتائج بحث أمازون`.
    """
    # 1. Search info bar bold text or heading
    info_selectors = (
        '[data-component-type="s-result-info-bar"] .a-color-state.a-text-bold',
        "#search .a-color-state.a-text-bold",
        ".s-desktop-toolbar .a-color-state.a-text-bold",
        "#search h1",
        "h1",
    )
    for sel in info_selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                txt = _clean_search_title_candidate(await el.inner_text())
                if txt:
                    return txt
        except Exception:
            continue

    # 2. URL `k` parameter (if human-readable, not an ASIN list)
    if affiliate_url:
        try:
            for k, v in parse_qsl(urlsplit(affiliate_url).query, keep_blank_values=False):
                if k.lower() in ("k", "field-keywords"):
                    candidate = _clean_search_title_candidate(v)
                    if candidate:
                        return candidate
        except Exception:
            pass

    # 3. Document title
    try:
        doc_title = _clean_search_title_candidate(await page.title())
        if doc_title:
            return doc_title
    except Exception:
        pass

    return FALLBACK_SEARCH_TITLE


async def _trigger_lazy_content(page) -> None:
    """Scroll gently down and back up to trigger lazy-loaded product card images."""
    try:
        await page.evaluate(
            """async () => {
                const delay = ms => new Promise(res => setTimeout(res, ms));
                const step = Math.max(window.innerHeight || 800, 600);
                const maxScroll = Math.min(document.body.scrollHeight || 2400, step * 3);
                for (let y = step; y <= maxScroll; y += step) {
                    window.scrollTo(0, y);
                    await delay(200);
                }
                window.scrollTo(0, 0);
                await delay(200);
            }"""
        )
    except Exception as exc:
        logger.debug("Lazy scroll on search page skipped: %s", exc)


async def hide_irrelevant_search_elements(page) -> None:
    """
    Inject CSS with `page.add_style_tag()` to hide Amazon headers, navbars, cookie banners,
    and footers before capturing the search-results screenshot.
    """
    try:
        await page.add_style_tag(content=SEARCH_SCREENSHOT_CSS)
    except Exception as exc:
        logger.debug("Failed to inject search screenshot CSS via add_style_tag: %s", exc)
        try:
            await page.evaluate(
                """(css) => {
                    const style = document.createElement('style');
                    style.type = 'text/css';
                    style.appendChild(document.createTextNode(css));
                    (document.head || document.documentElement).appendChild(style);
                }""",
                SEARCH_SCREENSHOT_CSS,
            )
        except Exception as eval_exc:
            logger.debug("Fallback CSS injection on search page also failed: %s", eval_exc)

    try:
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        pass


def _is_valid_box(
    box: dict[str, Any] | None,
    *,
    min_width: float = MIN_CONTAINER_WIDTH,
    min_height: float = MIN_CONTAINER_HEIGHT,
) -> bool:
    """Return True if a Playwright bounding box has valid positive dimensions above minimums."""
    if not box or not isinstance(box, dict):
        return False
    try:
        width = float(box.get("width", 0))
        height = float(box.get("height", 0))
        x = float(box.get("x", 0))
        y = float(box.get("y", 0))
    except (TypeError, ValueError):
        return False
    return width >= min_width and height >= min_height and x >= -10 and y >= -10


async def _find_search_container_locator(
    page,
) -> tuple[Any | None, dict[str, float] | None, str | None]:
    """
    Locate a primary search results container element that has a valid bounding box
    and contains visible product result cards.
    """
    for sel in SEARCH_CONTAINER_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if not el or not await el.is_visible():
                continue
            box = await el.bounding_box()
            if not _is_valid_box(box):
                continue

            has_cards = False
            if hasattr(el, "query_selector"):
                for card_sel in SEARCH_PRODUCT_CARD_SELECTORS:
                    try:
                        child = await el.query_selector(card_sel)
                        if child and await child.is_visible():
                            has_cards = True
                            break
                    except Exception:
                        continue
            else:
                has_cards = True

            if has_cards:
                clean_box = {
                    "x": float(box["x"]),
                    "y": float(box["y"]),
                    "width": float(box["width"]),
                    "height": float(box["height"]),
                }
                return el, clean_box, sel
        except Exception:
            continue

    return None, None, None


async def _calculate_search_bounding_box(
    page,
    container_box: dict[str, float] | None = None,
) -> dict[str, float] | None:
    """
    Calculate a safe clip bounding box covering the search result header/filters and
    product cards, capping height sensibly if the page is very long without cutting
    off the first row of products.
    """
    boxes: list[dict[str, float]] = []
    first_product_bottom: float | None = None

    for sel in SEARCH_HEADER_SELECTORS:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                b = await el.bounding_box()
                if _is_valid_box(b, min_width=100, min_height=20):
                    boxes.append(
                        {
                            "x": float(b["x"]),
                            "y": float(b["y"]),
                            "width": float(b["width"]),
                            "height": float(b["height"]),
                        }
                    )
        except Exception:
            continue

    for sel in SEARCH_PRODUCT_CARD_SELECTORS:
        try:
            elements = await page.query_selector_all(sel)
        except Exception:
            elements = []
        card_boxes: list[dict[str, float]] = []
        for el in elements:
            try:
                if not await el.is_visible():
                    continue
                b = await el.bounding_box()
                if _is_valid_box(b, min_width=80, min_height=80):
                    cb = {
                        "x": float(b["x"]),
                        "y": float(b["y"]),
                        "width": float(b["width"]),
                        "height": float(b["height"]),
                    }
                    card_boxes.append(cb)
                    bottom = cb["y"] + cb["height"]
                    if first_product_bottom is None or bottom < first_product_bottom:
                        first_product_bottom = bottom
            except Exception:
                continue
        if card_boxes:
            boxes.extend(card_boxes)
            break

    if container_box is not None and _is_valid_box(container_box):
        boxes.append(container_box)

    if not boxes:
        return None

    min_x = min(max(0.0, b["x"]) for b in boxes)
    min_y = min(max(0.0, b["y"]) for b in boxes)
    max_x = max(b["x"] + b["width"] for b in boxes)
    max_y = max(b["y"] + b["height"] for b in boxes)

    padding = 16.0
    clip_x = max(0.0, min_x - padding)
    clip_y = max(0.0, min_y - padding)
    clip_w = (max_x - clip_x) + padding
    clip_h = (max_y - clip_y) + padding

    if clip_h > MAX_FULL_PAGE_HEIGHT:
        min_safe_height = MAX_CAPTURE_HEIGHT
        if first_product_bottom is not None:
            first_product_rel_bottom = (first_product_bottom - clip_y) + padding
            min_safe_height = max(min_safe_height, first_product_rel_bottom)
        clip_h = min(clip_h, min_safe_height)

    if clip_w < MIN_CONTAINER_WIDTH or clip_h < MIN_CONTAINER_HEIGHT:
        return None

    return {
        "x": round(clip_x, 2),
        "y": round(clip_y, 2),
        "width": round(clip_w, 2),
        "height": round(clip_h, 2),
    }


def _validate_screenshot_file(screenshot_path: str) -> None:
    """
    Ensure the captured screenshot file exists, is non-empty, and is not a blank
    single-color image. Raises SearchScreenshotError on invalid/blank files.
    """
    if not screenshot_path or not os.path.isfile(screenshot_path):
        raise SearchScreenshotError(f"Search screenshot file does not exist: {screenshot_path}")

    size = os.path.getsize(screenshot_path)
    if size <= 0:
        raise SearchScreenshotError(f"Search screenshot file is empty (0 bytes): {screenshot_path}")

    # Check if file starts with PNG or JPEG signature; if so, verify it is not blank
    try:
        with open(screenshot_path, "rb") as f:
            header = f.read(8)
    except Exception as exc:
        raise SearchScreenshotError(f"Cannot read screenshot file {screenshot_path}: {exc}") from exc

    is_png = header.startswith(b"\x89PNG\r\n\x1a\n")
    is_jpeg = header.startswith(b"\xff\xd8\xff")
    if is_png or is_jpeg:
        try:
            with Image.open(screenshot_path) as img:
                width, height = img.size
                if width <= 0 or height <= 0:
                    raise SearchScreenshotError("Screenshot has invalid zero dimensions")
                extrema = img.convert("L").getextrema()
                if extrema is not None and extrema[0] == extrema[1]:
                    raise SearchScreenshotError(
                        f"Captured screenshot is completely blank (uniform pixel value={extrema[0]})"
                    )
        except SearchScreenshotError:
            raise
        except Exception as exc:
            raise SearchScreenshotError(f"Invalid image file at {screenshot_path}: {exc}") from exc


async def _capture_search_screenshot(page, screenshot_path: str) -> str:
    """
    Hide Amazon headers/footers and capture a clean screenshot focused on the search results:
    1. Prefer a locator screenshot when the search container is within `MAX_FULL_PAGE_HEIGHT`.
    2. Otherwise calculate a clip bounding box around search results and capture via `clip`.
    3. Fall back to page screenshot if bounding boxes are unavailable.
    Validates that the resulting file exists, is non-empty, and is not blank.
    """
    await hide_irrelevant_search_elements(page)

    container_el, container_box, container_sel = await _find_search_container_locator(page)

    try:
        if (
            container_el is not None
            and container_box is not None
            and container_box["height"] <= MAX_FULL_PAGE_HEIGHT
            and callable(getattr(container_el, "screenshot", None))
        ):
            logger.info(
                "SEARCH SCREENSHOT CAPTURE mode=locator selector=%s path=%s box=%s",
                container_sel,
                screenshot_path,
                container_box,
            )
            await container_el.screenshot(path=screenshot_path)
        else:
            clip_box = await _calculate_search_bounding_box(page, container_box=container_box)
            if clip_box is not None:
                logger.info(
                    "SEARCH SCREENSHOT CAPTURE mode=clip path=%s clip=%s",
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
                    "SEARCH SCREENSHOT CAPTURE mode=page_fallback path=%s page_height=%s full_page=%s",
                    screenshot_path,
                    page_height,
                    use_full_page,
                )
                await page.screenshot(path=screenshot_path, full_page=use_full_page)
    except Exception as exc:
        cleanup_files([screenshot_path])
        raise SearchScreenshotError(f"Failed to capture search screenshot: {exc}") from exc

    try:
        _validate_screenshot_file(screenshot_path)
    except Exception:
        cleanup_files([screenshot_path])
        raise

    return screenshot_path


async def scrape_search_page(
    browser_mgr: BrowserManager,
    affiliate_url: str,
    scrape_key: str,
) -> dict[str, Any]:
    """
    Open the Amazon search-results page using the shared BrowserManager, wait for
    product results to render, validate non-empty results, extract a search title,
    and capture a clean cropped screenshot.
    """
    if browser_mgr is None or not getattr(browser_mgr, "_browser", None):
        raise SearchNavigationError("Shared browser is not started")

    browser = browser_mgr._browser
    context = await browser.new_context(
        viewport={"width": SEARCH_VIEWPORT_WIDTH, "height": SEARCH_VIEWPORT_HEIGHT},
        user_agent=USER_AGENT,
        locale="ar-EG",
        timezone_id="Africa/Cairo",
        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},
    )
    page = await context.new_page()
    screenshot_path = f"{scrape_key}.png"

    try:
        last_nav_err: Exception | None = None
        for attempt in range(2):
            try:
                response = await page.goto(
                    affiliate_url,
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                if response is not None and getattr(response, "status", 200) >= 400:
                    raise SearchNavigationError(
                        f"HTTP {response.status} when loading search page {affiliate_url}"
                    )
                last_nav_err = None
                break
            except SearchNavigationError:
                raise
            except Exception as exc:
                last_nav_err = exc
                if attempt == 0:
                    logger.warning("Search page.goto failed (attempt 1), retrying: %s", exc)
                else:
                    raise SearchNavigationError(
                        f"Navigation failed for search URL {affiliate_url}: {exc}"
                    ) from exc

        if last_nav_err is not None:
            raise SearchNavigationError(f"Navigation failed: {last_nav_err}") from last_nav_err

        await _bypass_continue_shopping_if_present(page)
        await _check_page_unavailable_or_captcha(page)

        try:
            await page.wait_for_selector(
                '[data-component-type="s-search-result"], .s-main-slot .s-result-item, #search',
                state="attached",
                timeout=10000,
            )
        except Exception:
            logger.debug("Search primary selector wait timed out for url=%s; continuing", affiliate_url)

        await page.wait_for_timeout(1500)
        await _trigger_lazy_content(page)

        result_count = await _check_empty_search_results(page)
        title = await extract_search_title(page, affiliate_url=affiliate_url)
        verified_screenshot = await _capture_search_screenshot(page, screenshot_path)

        return {
            "affiliate_url": affiliate_url,
            "title": title,
            "result_count": result_count,
            "screenshot": verified_screenshot,
        }
    except Exception:
        if os.path.exists(screenshot_path):
            cleanup_files([screenshot_path])
        raise
    finally:
        await context.close()


def build_search_caption(
    title: str,
    affiliate_url: str,
) -> str:
    """
    Build the Telegram caption for an Amazon search-results post.
    Includes the extracted search title (or fallback) and the full affiliate search URL.
    """
    clean_title = (title or "").strip() or FALLBACK_SEARCH_TITLE
    lines = [
        "🔎 عروض ونتائج بحث من أمازون",
        "",
        f"🛍️ {clean_title}",
        "",
        "🔗 لينك العرض:",
        affiliate_url,
    ]
    return "\n".join(lines)


async def process_search_url(
    browser: BrowserManager,
    db: Any,
    url: str,
    *,
    scrape_key: str | None = None,
    domain: str = AMAZON_DOMAIN,
    affiliate_tag: str | None = None,
) -> dict[str, Any]:
    """
    End-to-end search-results URL processing flow:
    1. Validate and normalize the Amazon search URL, setting `k=LoqtaBGD` and
       `tag=<default_affiliate_tag>` while preserving all other query parameters.
    2. Compute deterministic `search_key` (`SEARCH:<hash>`) for duplicate checks.
    3. Open the page using the shared Playwright BrowserManager, wait for product cards,
       hide headers/footers, and capture a cropped screenshot.
    4. Build the Telegram caption with the affiliate search URL.
    """
    affiliate_url = normalize_search_url(
        url,
        domain=domain,
        affiliate_tag=affiliate_tag,
        db=db,
    )
    search_key = compute_search_key(affiliate_url)
    short_hash = search_key.split(":", 1)[-1].lower()

    logger.info(
        "SEARCH URL NORMALIZED original_url=%s affiliate_url=%s search_key=%s",
        url,
        affiliate_url,
        search_key,
    )

    if not scrape_key:
        scrape_key = f"search_{short_hash}_{int(time.time() * 1000)}"

    try:
        scraped = await scrape_search_page(browser, affiliate_url, scrape_key)
    except Exception as exc:
        logger.error(
            "SEARCH SCREENSHOT/SCRAPE FAILURE original_url=%s affiliate_url=%s error=%s",
            url,
            affiliate_url,
            exc,
        )
        raise

    screenshot_path = scraped["screenshot"]
    try:
        caption = build_search_caption(scraped["title"], affiliate_url)
        return {
            "search_key": search_key,
            "original_url": url,
            "normalized_url": affiliate_url,
            "affiliate_url": affiliate_url,
            "display_url": affiliate_url,
            "title": scraped["title"],
            "result_count": scraped["result_count"],
            "screenshot": screenshot_path,
            "caption": caption,
        }
    except Exception:
        cleanup_files([screenshot_path])
        raise


async def publish_search_to_telegram(
    application: Any,
    db: Any,
    search_data: dict[str, Any],
    *,
    source_channel_id: int = 0,
    cleanup_after: bool = True,
) -> bool:
    """
    Publish a processed search-results post to enabled Telegram destination channels.
    Enforces duplicate detection and moderation rules: if the same search query was
    published in the last `LAST_PUBLISHED_LOOKBACK` posts, holds the post for admin approval.
    """
    screenshot_path = search_data.get("screenshot")
    if not screenshot_path or not os.path.isfile(screenshot_path):
        raise SearchScreenshotError("Cannot publish search results: screenshot file does not exist")

    raw_url = (
        search_data.get("affiliate_url")
        or search_data.get("display_url")
        or search_data.get("normalized_url")
        or search_data.get("original_url")
        or ""
    )
    affiliate_url = (
        normalize_search_url(raw_url, db=db)
        if is_search_url(raw_url)
        else raw_url
    )
    search_key = search_data.get("search_key") or compute_search_key(affiliate_url)
    title = search_data.get("title") or FALLBACK_SEARCH_TITLE
    raw_caption = search_data.get("caption") or ""
    if raw_caption and raw_url and raw_url != affiliate_url and raw_url in raw_caption:
        caption = raw_caption.replace(raw_url, affiliate_url)
    else:
        caption = raw_caption or build_search_caption(title, affiliate_url)

    # Enforce duplicate check & admin moderation rules
    if db is not None and hasattr(db, "is_asin_in_last_published"):
        if db.is_asin_in_last_published(search_key, LAST_PUBLISHED_LOOKBACK, source="search"):
            logger.info(
                "DUPLICATE SEARCH DETECTED search_key=%s affiliate_url=%s — holding for admin approval",
                search_key,
                affiliate_url,
            )
            pending_id = db.create_pending_approval(
                asin=search_key,
                title=title,
                price="SEARCH",
                clean_url=affiliate_url,
                source_channel_id=source_channel_id,
                caption=caption,
                image_path=screenshot_path,
                seller_type="NEW_AMAZON",
            )
            await send_approval_request(
                application.bot,
                db,
                pending_id,
                search_key,
                title,
                source_channel_id,
                screenshot_path,
                price="SEARCH",
            )
            return False

    temp_files: list[str] = [screenshot_path] if cleanup_after else []
    try:
        upload_image = to_jpeg_for_telegram(screenshot_path)
        if upload_image != screenshot_path:
            temp_files.append(upload_image)

        products = [{"title": title, "url": affiliate_url}]
        # Only include product URL button if within Telegram's 512-byte inline button URL limit
        button_products = (
            products
            if len(affiliate_url.encode("utf-8")) <= MAX_INLINE_BUTTON_URL_BYTES
            else []
        )
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

        destinations = db.get_enabled_destinations()
        if not destinations:
            logger.error("No enabled destinations configured for search publish")
            return False

        result = await publish_to_destinations(
            application.bot,
            destinations,
            upload_image,
            caption,
            reply_markup=inline_keyboard if inline_keyboard.inline_keyboard else None,
            products=products,
            publish_type="SEARCH",
        )
        result.log_summary()
        if result.successful <= 0:
            logger.error(
                "SEARCH PUBLISH OUTCOME status=FAILED original_url=%s affiliate_url=%s",
                search_data.get("original_url"),
                affiliate_url,
            )
            raise SearchPublishError("Failed to upload search screenshot to any Telegram destination")

        if hasattr(db, "add_published_product"):
            for publish_result in result.results:
                if publish_result.success:
                    db.add_published_product(
                        search_key,
                        title,
                        source_channel_id,
                        publish_result.message_id,
                        destination_id=publish_result.destination_id,
                        seller_type="NEW_AMAZON",
                        image_path=screenshot_path,
                        clean_url=affiliate_url,
                        published_price="SEARCH",
                    )

        logger.info(
            "SEARCH PUBLISH OUTCOME status=SUCCESS search_key=%s original_url=%s affiliate_url=%s destinations=%d/%d",
            search_key,
            search_data.get("original_url"),
            affiliate_url,
            result.successful,
            result.total,
        )
        return True
    finally:
        if temp_files:
            cleanup_files(temp_files)
