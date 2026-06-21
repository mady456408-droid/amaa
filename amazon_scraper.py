"""
Visual scrape logic — frozen to original base code.
Infrastructure only: shared Chromium process + fresh context per scrape.
"""

import logging
import os
import time

from playwright.async_api import Browser, Playwright, async_playwright

from telegram import Bot
from config import BOT_TOKEN, ADMIN_USER_IDS
import datetime

from config import CHROMIUM_ARGS, PRICE_SELECTORS, TITLE_SELECTORS, USER_AGENT
from coupon_normalize import normalize_coupon_text
from coupon_price import parse_price_number

logger = logging.getLogger(__name__)

_ACCEPT_LANGUAGE = "ar-EG,ar;q=0.9,en;q=0.8"

# Single Bot instance for failure artifact delivery
_FAILURE_BOT = Bot(token=BOT_TOKEN) if BOT_TOKEN else None

# Priority 1 — Amazon clip-coupon widget (checkbox / Apply coupon)
WIDGET_COUPON_SELECTORS = [
    ".newCouponBadge",
    ".couponLabelText",
    "[id*='couponText']",
    "#couponText",
    ".vpc_coupon_message",
    ".couponBadge",
    ".apexCouponRow",
    "[data-csa-c-content-id='couponFeature']",
]

# Priority 2 — promo coupon blocks
PROMO_COUPON_SELECTORS = [
    "[id*='coupon_feature_div']",
    "#coupon_feature_div",
    "[id*='promoPriceBlockMessage']",
    "#promoPriceBlockMessage",
    ".promoPriceBlockMessage",
    "[data-csa-c-owner='PromotionsDiscovery']",
]

# Priority 3 — broader promo areas (still requires real coupon text)
GENERIC_COUPON_SELECTORS = [
    "[data-csa-c-content-id*='coupon']",
    "#vpc-coupon-text",
]

# Sale / pay price (prefer over generic PRICE_SELECTORS)
DISPLAY_PRICE_SELECTORS = [
    "#apexPriceToPay .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    ".reinventPricePriceToPayMargin .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
]

# Strikethrough / was price
LIST_PRICE_SELECTORS = [
    "#corePrice_feature_div .a-text-price .a-offscreen",
    "#apex_price .a-text-price .a-offscreen",
    "span.a-price[data-a-strike='true'] .a-offscreen",
    ".a-text-price .a-offscreen",
]

COUPON_APPLIED_SEARCH_SELECTORS = [
    "#coupon_feature_div",
    "#promoPriceBlockMessage",
    "[id*='coupon_feature_div']",
    "#corePrice_feature_div",
    "#apexPriceToPay",
]

COUPON_APPLIED_MARKERS = (
    "coupon applied",
    "off coupon applied",
    "with coupon",
    "كوبون مطبق",
    "تم تطبيق الكوبون",
    "تم تطبيق",
    "بعد الكوبون",
    "سعر الكوبون",
)


class BrowserManager:
    """Reuses Chromium process only — fresh context per scrape."""

    def __init__(self):
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._browser:
            return
        logger.info("Starting shared Chromium process")
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_ARGS,
        )
        logger.info("Chromium process ready")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Chromium process stopped")


async def _wait_for_product_title(page) -> bool:
    for selector in TITLE_SELECTORS:
        try:
            await page.wait_for_selector(selector, state="attached", timeout=10000)
            logger.info("TITLE SELECTOR FOUND: %s", selector)
            return True
        except Exception:
            continue
    # Log selector search failure
    logger.error(
        "TITLE SELECTOR SEARCH FAILED\n"
        "asin=%s\n"
        "tried_selectors=%s",
        page.url.split("/dp/")[-1].split("/")[0] if "/dp/" in page.url else "unknown",
        TITLE_SELECTORS,
    )
    return False


async def _first_selector_text(page, selectors: list[str], *, label: str) -> tuple[str, str | None]:
    for selector in selectors:
        locator = page.locator(selector)
        if await locator.count() > 0:
            txt = await locator.first.text_content()
            if txt and txt.strip():
                logger.info("%s SELECTOR FOUND: %s -> %r", label, selector, txt.strip())
                return txt.strip(), selector
    return "Not found", None


async def _extract_list_price(page) -> str | None:
    for selector in LIST_PRICE_SELECTORS:
        locator = page.locator(selector)
        count = await locator.count()
        for i in range(min(count, 3)):
            txt = await locator.nth(i).text_content()
            if txt and txt.strip():
                logger.info("LIST PRICE SELECTOR FOUND: %s -> %r", selector, txt.strip())
                return txt.strip()
    return None


async def _detect_coupon_already_applied(page) -> bool:
    for selector in COUPON_APPLIED_SEARCH_SELECTORS:
        locator = page.locator(selector)
        if await locator.count() == 0:
            continue
        try:
            text = await locator.first.inner_text()
        except Exception:
            continue
        if not text:
            continue
        lower = text.lower()
        for marker in COUPON_APPLIED_MARKERS:
            if marker.lower() in lower:
                logger.info(
                    "COUPON ALREADY APPLIED marker=%r selector=%s",
                    marker,
                    selector,
                )
                return True
    return False


async def _extract_title_and_price(page) -> tuple[str, str, str | None]:
    title = "Not found"
    price = "Not found"
    list_price: str | None = None

    for selector in TITLE_SELECTORS:
        locator = page.locator(selector)
        if await locator.count() > 0:
            txt = await locator.first.text_content()
            if txt and txt.strip():
                title = txt.strip()
                logger.info("TITLE SELECTOR FOUND: %s", selector)
                break

    price, _ = await _first_selector_text(
        page, DISPLAY_PRICE_SELECTORS, label="DISPLAY PRICE"
    )
    if price == "Not found":
        price, _ = await _first_selector_text(page, PRICE_SELECTORS, label="PRICE")

    list_price = await _extract_list_price(page)
    if list_price and price != "Not found":
        display_n = parse_price_number(price)
        list_n = parse_price_number(list_price)
        if display_n is not None and list_n is not None and abs(display_n - list_n) < 0.01:
            list_price = None

    return title, price, list_price


async def _collect_text_snippets(locator) -> list[str]:
    """Gather text from widget, parent container, and nested label nodes."""
    snippets: list[str] = []
    seen: set[str] = set()

    def add(text: str | None) -> None:
        if not text:
            return
        t = text.strip()
        if t and t not in seen:
            seen.add(t)
            snippets.append(t)

    # Parent container first (Amazon splits "Coupon:" + "Apply EGP60 coupon")
    try:
        parent_text = await locator.evaluate(
            """el => {
                let node = el;
                for (let i = 0; i < 4 && node; i++) {
                    const cls = (node.className || '').toString().toLowerCase();
                    const id = (node.id || '').toString().toLowerCase();
                    if (cls.includes('coupon') || id.includes('coupon')
                        || cls.includes('promo') || id.includes('promo')) {
                        return (node.innerText || node.textContent || '').trim();
                    }
                    node = node.parentElement;
                }
                const p = el.parentElement;
                return p ? (p.innerText || p.textContent || '').trim() : '';
            }"""
        )
        add(parent_text)
    except Exception:
        pass

    try:
        txt = await locator.inner_text()
        add(txt)
    except Exception:
        try:
            txt = await locator.text_content()
            add(txt)
        except Exception:
            pass

    try:
        labels = locator.locator(".couponLabelText, .newCouponBadge")
        count = await labels.count()
        for i in range(min(count, 5)):
            t = await labels.nth(i).inner_text()
            add(t)
    except Exception:
        pass

    return snippets


def _accept_snippets(snippets: list[str], *, source: str) -> str | None:
    if not snippets:
        return None
    # Try merged widget text first (handles split Coupon: + Apply lines)
    merged = " ".join(snippets)
    normalized = normalize_coupon_text(merged)
    if normalized:
        return normalized
    for raw in snippets:
        normalized = normalize_coupon_text(raw)
        if normalized:
            return normalized
    logger.info("COUPON REJECTED (invalid pattern) source=%s", source)
    return None


async def _extract_from_selector_tier(
    page,
    selectors: list[str],
    *,
    tier: str,
) -> str | None:
    for selector in selectors:
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            continue

        for i in range(min(count, 3)):
            snippets = await _collect_text_snippets(locator.nth(i))
            if not snippets:
                continue
            if tier == "widget":
                logger.info("REAL COUPON WIDGET: %s", selector)
            elif tier == "promo":
                logger.info("PROMO COUPON BLOCK: %s", selector)
            else:
                logger.info("GENERIC COUPON BLOCK: %s", selector)

            accepted = _accept_snippets(
                snippets, source=f"{tier}:{selector}"
            )
            if accepted:
                logger.info("COUPON SELECTOR FOUND: %s", selector)
                return accepted
    return None


async def _extract_coupon(page, *, enabled: bool) -> str | None:
    if not enabled:
        logger.info("COUPON DETECTION DISABLED")
        return None

    coupon = await _extract_from_selector_tier(
        page, WIDGET_COUPON_SELECTORS, tier="widget"
    )
    if not coupon:
        coupon = await _extract_from_selector_tier(
            page, PROMO_COUPON_SELECTORS, tier="promo"
        )
    if not coupon:
        coupon = await _extract_from_selector_tier(
            page, GENERIC_COUPON_SELECTORS, tier="generic"
        )

    if not coupon:
        logger.info("COUPON NOT FOUND")
        return None
    return coupon


async def _send_failure_artifacts(
    asin: str,
    url: str,
    html_path: str | None = None,
    screenshot_path: str | None = None,
) -> None:
    """Send failure artifacts to Telegram admins without raising exceptions."""
    if not _FAILURE_BOT or not ADMIN_USER_IDS:
        logger.warning("FAILURE ARTIFACT SEND SKIPPED: BOT_TOKEN or ADMIN_USER_IDS not configured")
        return

    html_sent = False
    screenshot_sent = False

    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S UTC")
        caption = f"🚨 Scrape Failure\n\nASIN: {asin}\nURL: {url}\nTimestamp: {timestamp}"

        for admin_id in ADMIN_USER_IDS:
            if html_path and os.path.exists(html_path):
                try:
                    with open(html_path, "rb") as f:
                        await _FAILURE_BOT.send_document(
                            chat_id=admin_id,
                            document=f,
                            caption=f"{caption}\n\nType: HTML dump",
                            read_timeout=30,
                            write_timeout=30,
                        )
                    logger.info("FAILURE ARTIFACT SENT type=html admin=%s", admin_id)
                    html_sent = True
                except Exception as exc:
                    logger.error("FAILURE ARTIFACT SEND FAILED type=html admin=%s error=%s", admin_id, exc)
                    logger.error("FAILURE ARTIFACT RETAINED path=%s", html_path)

            if screenshot_path and os.path.exists(screenshot_path):
                try:
                    with open(screenshot_path, "rb") as f:
                        await _FAILURE_BOT.send_photo(
                            chat_id=admin_id,
                            photo=f,
                            caption=f"{caption}\n\nType: Screenshot",
                            read_timeout=30,
                            write_timeout=30,
                        )
                    logger.info("FAILURE ARTIFACT SENT type=screenshot admin=%s", admin_id)
                    screenshot_sent = True
                except Exception as exc:
                    logger.error("FAILURE ARTIFACT SEND FAILED type=screenshot admin=%s error=%s", admin_id, exc)
                    logger.error("FAILURE ARTIFACT RETAINED path=%s", screenshot_path)

        # Cleanup only if delivery succeeded
        if html_sent and html_path and os.path.exists(html_path):
            try:
                os.remove(html_path)
                logger.info("FAILURE ARTIFACT CLEANUP path=%s", html_path)
            except Exception as exc:
                logger.error("FAILURE ARTIFACT CLEANUP FAILED path=%s error=%s", html_path, exc)

        if screenshot_sent and screenshot_path and os.path.exists(screenshot_path):
            try:
                os.remove(screenshot_path)
                logger.info("FAILURE ARTIFACT CLEANUP path=%s", screenshot_path)
            except Exception as exc:
                logger.error("FAILURE ARTIFACT CLEANUP FAILED path=%s error=%s", screenshot_path, exc)

    except Exception as exc:
        logger.error("FAILURE ARTIFACT SEND FAILED error=%s", exc)


async def scrape_coupon_and_screenshot(
    browser_mgr: BrowserManager,
    clean_url: str,
    asin: str,
    *,
    coupon_detection_enabled: bool = True,
    capture_screenshot: bool = True,
) -> dict:
    """
    Lightweight Playwright pass — coupon detection and optional screenshot only.
    Does not scrape title or price (Creators API supplies those).
    """
    browser = browser_mgr._browser
    if not browser:
        raise RuntimeError("Browser not started")

    context = await browser.new_context(
        viewport={"width": 2000, "height": 1220},
        user_agent=USER_AGENT,
        locale="ar-EG",
        timezone_id="Africa/Cairo",
        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},
    )
    page = await context.new_page()
    screenshot_path = f"{asin}_coupon.png" if capture_screenshot else None

    try:
        await page.goto(clean_url, timeout=30000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await _wait_for_product_title(page)

        list_price = await _extract_list_price(page)
        coupon = await _extract_coupon(page, enabled=coupon_detection_enabled)
        coupon_already_applied = False
        if coupon and coupon_detection_enabled:
            coupon_already_applied = await _detect_coupon_already_applied(page)

        if capture_screenshot and screenshot_path:
            await page.evaluate("document.body.style.zoom = '130%'")
            await page.wait_for_timeout(2000)
            await page.screenshot(path=screenshot_path, full_page=False)
            logger.info("COUPON SCAN SUCCESS screenshot=%s coupon=%r", screenshot_path, coupon)
        elif coupon_detection_enabled:
            if coupon:
                logger.info("COUPON SCAN SUCCESS coupon=%r", coupon)
            else:
                logger.info("COUPON SCAN: no coupon found")

        return {
            "coupon": coupon,
            "coupon_already_applied": coupon_already_applied,
            "list_price": list_price,
            "screenshot": screenshot_path,
        }
    except Exception:
        logger.exception("COUPON SCAN FAILED asin=%s", asin)
        return {
            "coupon": None,
            "coupon_already_applied": False,
            "list_price": None,
            "screenshot": screenshot_path if capture_screenshot else None,
        }
    finally:
        await context.close()


async def scrape_amazon(
    browser_mgr: BrowserManager,
    clean_url: str,
    asin: str,
    *,
    coupon_detection_enabled: bool = True,
) -> dict:
    browser = browser_mgr._browser
    if not browser:
        raise RuntimeError("Browser not started")

    context = await browser.new_context(
        viewport={"width": 2000, "height": 1220},
        user_agent=USER_AGENT,
        locale="ar-EG",
        timezone_id="Africa/Cairo",
        extra_http_headers={"Accept-Language": _ACCEPT_LANGUAGE},
    )
    page = await context.new_page()
    screenshot_path = f"{asin}.png"

    try:
        for attempt in range(2):
            try:
                await page.goto(
                    clean_url,
                    timeout=30000,
                    wait_until="domcontentloaded",
                )
                break
            except Exception as exc:
                if attempt == 0:
                    logger.warning("page.goto failed, retrying once: %s", exc)
                else:
                    raise

        # Detect Continue Shopping interstitial
        try:
            continue_button = page.locator(
                "button.a-button-text[alt*='متابعة التسوق']"
            )

            await continue_button.first.wait_for(
                state="visible",
                timeout=5000,
            )

            logger.warning(
                "CONTINUE SHOPPING PAGE DETECTED asin=%s",
                asin,
            )

            await continue_button.first.click()

            logger.info(
                "CONTINUE SHOPPING RECOVERY START asin=%s",
                asin,
            )
            recovery_start = time.time()

            # Wait for product page signals instead of networkidle
            try:
                await page.wait_for_selector(
                    "#landingImage, #imgTagWrapperId img, #productTitle",
                    timeout=6000,
                )
            except Exception:
                logger.warning(
                    "CONTINUE SHOPPING RECOVERY SIGNAL NOT FOUND asin=%s",
                    asin,
                )

            recovery_time = time.time() - recovery_start
            logger.info(
                "CONTINUE SHOPPING RECOVERY COMPLETE asin=%s took=%.2fs",
                asin,
                recovery_time,
            )

            logger.info(
                "CONTINUE SHOPPING PAGE BYPASSED asin=%s final_url=%s",
                asin,
                page.url,
            )

        except Exception:
            pass

        await page.wait_for_timeout(3000)

        await page.evaluate("""
            document.body.style.zoom = '130%'
        """)

        await page.wait_for_timeout(2000)
        await _wait_for_product_title(page)
        title, price, list_price = await _extract_title_and_price(page)
        if title == "Not found":
            logger.info("SCRAPE RETRY")
            await page.wait_for_timeout(3000)
            await _wait_for_product_title(page)
            retry_title, retry_price, retry_list = await _extract_title_and_price(page)
            if retry_title != "Not found":
                title = retry_title
            if price == "Not found" and retry_price != "Not found":
                price = retry_price
            if not list_price and retry_list:
                list_price = retry_list

        html_dump_path = None
        if title == "Not found":
            page_url = page.url
            page_title = await page.title()
            body_text = await page.inner_text("body")
            html = await page.content()
            
            # Save full HTML to disk
            html_dump_path = f"failure_{asin}.html"
            with open(html_dump_path, "w", encoding="utf-8") as f:
                f.write(html)
            logger.error(
                "FAILURE HTML SAVED path=%s",
                html_dump_path,
            )
            

            logger.error(
                "SCRAPE FAILURE DIAGNOSTICS:\n"
                "asin=%s\n"
                "url=%s\n"
                "page_title=%r\n"
                "BODY PREVIEW=%r\n"
                "HTML PREVIEW=%r",
                asin,
                page_url,
                page_title,
                body_text[:2000],
                html[:2000],
            )
            logger.error(
                "URL ANALYSIS:\n"
                "current_url=%s\n"
                "contains_amazon=%s\n"
                "contains_captcha=%s\n"
                "contains_ap_signin=%s\n"
                "contains_errors=%s",
                page.url,
                "amazon" in page.url.lower(),
                "captcha" in page.url.lower(),
                "ap/signin" in page.url.lower(),
                "/errors/" in page.url.lower(),
            )
            logger.error(
                "PAGE INDICATORS:\n"
                "captcha=%s\n"
                "robot_check=%s\n"
                "sign_in=%s\n"
                "sorry_page=%s\n"
                "503_service_unavailable=%s\n"
                "automated_access=%s\n"
                "product_title_exists=%s",
                "captcha" in html.lower(),
                "robot check" in html.lower(),
                "sign in" in html.lower(),
                "sorry" in html.lower(),
                "503" in html.lower(),
                "automated access" in html.lower(),
                await page.locator("#productTitle").count() > 0,
            )

        coupon = await _extract_coupon(
            page, enabled=coupon_detection_enabled
        )
        coupon_already_applied = False
        if coupon and coupon_detection_enabled:
            coupon_already_applied = await _detect_coupon_already_applied(page)

        if title != "Not found":
            await page.screenshot(
                path=screenshot_path,
                full_page=False,
            )
            logger.info("SCRAPE SUCCESS")

        logger.info("Scraped: %s %s coupon=%s", title, price, coupon)
        logger.info(
            "SCRAPER DEBUG title=%r price=%r list_price=%r coupon=%r "
            "coupon_already_applied=%s",
            title,
            price,
            list_price,
            coupon,
            coupon_already_applied,
        )

        if not screenshot_path or not os.path.exists(screenshot_path):
            failure_path = f"failure_{asin}.png"
            await page.screenshot(path=failure_path, full_page=True)
            logger.error(
                "FAILURE SCREENSHOT SAVED path=%s",
                failure_path,
            )

            await _send_failure_artifacts(
                asin=asin,
                url=page.url,
                html_path=html_dump_path,
                screenshot_path=failure_path,
            )

            logger.error(
                "SCREENSHOT GENERATION FAILED path=%s asin=%s",
                screenshot_path,
                asin,
            )
            raise RuntimeError(f"Screenshot generation failed for ASIN {asin}")

        return {
            "title": title,
            "price": price,
            "list_price": list_price,
            "coupon": coupon,
            "coupon_already_applied": coupon_already_applied,
            "screenshot": screenshot_path,
        }
    finally:
        await context.close()
