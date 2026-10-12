import logging
import re

import httpx
from telegram import Message
try:
    from telegram.constants import MessageEntityType
except ImportError:
    from telegram import MessageEntity as MessageEntityType
    import telegram.constants
    telegram.constants.MessageEntityType = MessageEntityType

try:
    import telegram.ext as _tg_ext
    if not hasattr(_tg_ext, "ContextTypes"):
        class _ContextTypes:
            DEFAULT_TYPE = object
        _tg_ext.ContextTypes = _ContextTypes
    elif not hasattr(_tg_ext.ContextTypes, "DEFAULT_TYPE"):
        _tg_ext.ContextTypes.DEFAULT_TYPE = object
    if not hasattr(_tg_ext, "ApplicationBuilder"):
        _tg_ext.ApplicationBuilder = object
    if not hasattr(_tg_ext, "filters") and hasattr(_tg_ext, "Filters"):
        _tg_ext.filters = _tg_ext.Filters
    from telegram import InlineKeyboardButton as _IKB
    if _IKB.__repr__ is object.__repr__:
        _IKB.__repr__ = lambda self: (
            f"InlineKeyboardButton(text={self.text!r}, callback_data={self.callback_data!r}, url={self.url!r})"
        )
except Exception:
    pass

from config import REDIRECT_TIMEOUT_SEC, USER_AGENT
from promotion_handler import (
    extract_promotion_id,
    is_promotion_url,
    normalize_promotion_url,
)
from search_handler import is_search_url

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.I)

ASIN_PATTERNS = [
    r"/dp/([A-Z0-9]{10})",
    r"/gp/product/([A-Z0-9]{10})",
    r"/gp/aw/d/([A-Z0-9]{10})",
    r"/product/([A-Z0-9]{10})",
    r"[?&]asin=([A-Z0-9]{10})",
]

ASIN_ONLY_PATTERN = re.compile(r"\b([A-Z0-9]{10})\b", re.I)

# Strict match: the entire message (stripped) is exactly a 10-char ASIN.
ASIN_STRICT_PATTERN = re.compile(r"^[A-Z0-9]{10}$", re.I)

_http_client: httpx.AsyncClient | None = None


async def init_http_client() -> None:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(REDIRECT_TIMEOUT_SEC, connect=5.0),
            headers={"User-Agent": USER_AGENT},
        )
        logger.info("HTTP redirect client ready")


async def close_http_client() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None
        logger.info("HTTP redirect client closed")


def get_message_text(msg: Message) -> str:
    return getattr(msg, "text", None) or getattr(msg, "caption", None) or ""


def _normalize_url(url: str) -> str:
    return url.strip().rstrip(".,)>]")


def extract_all_urls_from_text(text: str) -> list[str]:
    """Extract all URLs from text using findall, deduplicated in order."""
    if not text:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for match in URL_PATTERN.findall(text.strip()):
        url = _normalize_url(match)
        key = url.lower()
        if key and key not in seen:
            seen.add(key)
            urls.append(url)
    return urls


def extract_all_urls_from_message(msg: Message) -> list[str]:
    """All URLs from plain text, caption, and Telegram entities."""
    precomputed = getattr(msg, "urls", None)
    if precomputed is not None:
        return list(precomputed)

    text = get_message_text(msg)
    seen: set[str] = {u.lower() for u in extract_all_urls_from_text(text)}
    urls: list[str] = extract_all_urls_from_text(text)

    entities = msg.entities or msg.caption_entities or []
    for ent in entities:
        if ent.type == MessageEntityType.URL and text:
            url = _normalize_url(text[ent.offset : ent.offset + ent.length])
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)
        elif ent.type == MessageEntityType.TEXT_LINK and ent.url:
            url = _normalize_url(ent.url)
            key = url.lower()
            if key and key not in seen:
                seen.add(key)
                urls.append(url)

    return urls


def extract_url_from_message(msg: Message) -> str | None:
    """First URL only (backward compatible)."""
    urls = extract_all_urls_from_message(msg)
    return urls[0] if urls else None


def extract_asin(url: str) -> str | None:
    if is_promotion_url(url) or is_search_url(url):
        return None
    for pattern in ASIN_PATTERNS:
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).upper()
    return None


def is_standalone_asin(text: str) -> str | None:
    """Return ASIN if text is a single 10-char product id."""
    token = text.strip().upper()
    if ASIN_ONLY_PATTERN.fullmatch(token):
        return token
    return None


def is_http_url(text: str) -> bool:
    """True when text is an http(s) URL."""
    return text.strip().lower().startswith(("http://", "https://"))


AMAZON_DOMAINS = re.compile(
    r"amazon\.(com|co\.uk|de|fr|it|es|ca|com\.au|com\.br|co\.jp|in|eg|sa|ae|nl|se|pl|sg|tr|mx)",
    re.I,
)


def is_amazon_url(url: str) -> bool:
    """True when URL host/path indicates an Amazon marketplace page."""
    return bool(AMAZON_DOMAINS.search(url))


def is_manual_post_input(text: str) -> bool:
    """
    Return True only when the text is unambiguously a manual post request:
    - A single strict ASIN (entire stripped text matches ^[A-Z0-9]{10}$), OR
    - One or more http(s) URLs with no other non-whitespace text.

    Redirect expansion and Amazon validation happen later in resolve_asin_from_input().
    Paragraphs, sentences, or any other free-form text return False.
    """
    stripped = text.strip()
    if not stripped:
        return False

    # Strict ASIN: full text is exactly 10 alphanumeric chars
    if ASIN_STRICT_PATTERN.match(stripped):
        return True

    # Must contain at least one URL
    urls = extract_all_urls_from_text(stripped)
    if not urls:
        return False

    # Every token (words) outside the URLs should be empty / whitespace only
    remaining = stripped
    for url in urls:
        remaining = remaining.replace(url, "")
    if remaining.strip():
        # There's non-URL text alongside the URLs — likely a paragraph with an embedded link
        return False

    # Accept any http(s) URL — unknown shorteners are resolved before ASIN extraction.
    return all(is_http_url(u) for u in urls)


def extract_manual_inputs(text: str) -> list[str]:
    """
    Extract URLs and standalone ASINs from admin manual input text.
    Returns URLs and bare ASIN strings, deduplicated in order.
    """
    if not text:
        return []
    urls = extract_all_urls_from_text(text)
    seen_asins: set[str] = set()
    for url in urls:
        asin = extract_asin(url)
        if asin:
            seen_asins.add(asin)

    remaining = text
    for url in urls:
        remaining = remaining.replace(url, " ")

    inputs: list[str] = []
    seen_keys: set[str] = set()

    for url in urls:
        key = url.lower()
        if key not in seen_keys:
            seen_keys.add(key)
            inputs.append(url)

    for match in ASIN_ONLY_PATTERN.finditer(remaining):
        asin = match.group(1).upper()
        if asin in seen_asins:
            continue
        key = f"asin:{asin}"
        if key not in seen_keys:
            seen_keys.add(key)
            seen_asins.add(asin)
            inputs.append(asin)

    return inputs


import contextvars
import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

NEW_AMAZON_SELLER_ID = "A1ZVRGNO5AYLOV"
AMAZON_RESALE_SELLER_ID = "A2N2MP47XAP1MK"

MAX_REDIRECT_HOPS = 10

_SHORTENER_HOSTS = frozenset({
    "amzn.to",
    "amzn.eu",
    "a.co",
    "3rrood.com",
    "www.3rrood.com",
    "a.y-ay.com",
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "cutt.ly",
    "is.gd",
    "rb.gy",
})

_UNSAFE_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata.google.internal",
    "metadata",
})

_SENSITIVE_QUERY_KEYS = frozenset({
    "token",
    "access_token",
    "refresh_token",
    "auth",
    "authorization",
    "password",
    "passwd",
    "secret",
    "client_secret",
    "api_key",
    "apikey",
    "session",
    "sessionid",
    "session_id",
    "cookie",
    " cookies",
    "sig",
    "signature",
})

_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=["\']?\s*\d+\s*;\s*url=([^"\'>\s]+)',
    re.IGNORECASE,
)

_active_browser_ctx: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "_active_browser_ctx", default=None
)
_last_resolution_method_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_last_resolution_method_ctx", default="unknown"
)


class LinkResolutionError(Exception):
    """Base exception for link resolution and destination validation failures."""

    def __init__(self, message: str, *, reason_code: str = "resolution_error"):
        super().__init__(message)
        self.reason_code = reason_code


class InvalidInputUrlError(LinkResolutionError):
    """Raised when the input is not a valid HTTP/HTTPS URL."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="invalid_input_url")


class UnsafeDestinationError(LinkResolutionError):
    """Raised when a URL or redirect hop targets an unsafe scheme, host, or private IP."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="unsafe_destination")


class RedirectLoopError(LinkResolutionError):
    """Raised when a redirect chain visits the same URL more than once."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="redirect_loop")


class RedirectLimitExceededError(LinkResolutionError):
    """Raised when a redirect chain exceeds MAX_REDIRECT_HOPS."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="redirect_limit_exceeded")


class RedirectTimeoutError(LinkResolutionError):
    """Raised when redirect resolution times out."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="redirect_timeout")


class RedirectResolutionError(LinkResolutionError):
    """Raised when HTTP/browser redirect resolution fails."""

    def __init__(self, message: str):
        super().__init__(message, reason_code="redirect_failed")


@dataclass
class ResolvedProductInput:
    asin: str
    merchant_id: str | None
    seller_type: str  # 'AMAZON_RESALE' or 'NEW_AMAZON'
    clean_url: str
    final_url: str | None = None


@dataclass
class ResolvedDestination:
    original_url: str
    normalized_url: str
    resolved_url: str | None
    resolution_method: str
    destination_type: str  # 'product', 'search', 'promotion', 'unsupported', 'error'
    selected_handler: str  # 'product_handler', 'search_handler', 'promotion_handler', 'none'
    asin: str | None = None
    product_resolution: ResolvedProductInput | None = None
    error_reason: str | None = None


def _redact_url_for_log(url: str | None) -> str:
    """Redact credentials and sensitive query parameters from URLs before logging."""
    if not url or not isinstance(url, str):
        return str(url)
    try:
        parsed = urlsplit(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return url.strip()
        netloc = parsed.netloc
        if "@" in netloc:
            netloc = "***@" + netloc.rsplit("@", 1)[-1]
        if parsed.query:
            items = parse_qsl(parsed.query, keep_blank_values=True)
            sanitized_items = [
                (k, "[REDACTED]" if k.lower().strip() in _SENSITIVE_QUERY_KEYS else v)
                for k, v in items
            ]
            query = urlencode(sanitized_items, doseq=True)
        else:
            query = ""
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except Exception:
        return "<unparseable_url>"


def check_destination_safety(url: str) -> tuple[bool, str | None]:
    """
    Validate that `url` uses http/https, has no embedded credentials, and does not
    target localhost, loopback, private networks, link-local, or reserved IP space.
    """
    if not url or not isinstance(url, str):
        return False, "Empty or non-string URL"
    raw = url.strip()
    try:
        parsed = urlsplit(raw)
    except Exception as exc:
        return False, f"Malformed URL: {exc}"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return False, f"Unsupported URL scheme: {scheme or 'missing'}"

    if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
        return False, "Credentials embedded in URL are not allowed"

    host = (parsed.hostname or "").lower().strip().rstrip(".")
    if not host:
        return False, "Missing hostname in URL"

    if (
        host in _UNSAFE_HOSTNAMES
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
    ):
        return False, f"Unsafe internal hostname: {host}"

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if (
            ip_obj.is_loopback
            or ip_obj.is_private
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        ):
            return False, f"Unsafe IP address destination: {ip_obj}"

    return True, None


def is_safe_destination_url(url: str) -> bool:
    """Return True if `url` passes all destination safety checks."""
    safe, _ = check_destination_safety(url)
    return safe


def normalize_input_url(url: str) -> str:
    """
    Step 1 of Link Resolution Pipeline:
    Validate and normalize an incoming HTTP/HTTPS URL.
    Raises InvalidInputUrlError or UnsafeDestinationError on invalid/unsafe input.
    """
    if not url or not isinstance(url, str):
        raise InvalidInputUrlError("Empty or non-string URL input")
    cleaned = _normalize_url(url)
    if not cleaned:
        raise InvalidInputUrlError("Whitespace-only URL input")
    if not is_http_url(cleaned):
        raise InvalidInputUrlError(f"Unsupported non-HTTP(S) URL: {_redact_url_for_log(cleaned)}")

    safe, reason = check_destination_safety(cleaned)
    if not safe:
        if reason and ("Unsupported URL scheme" in reason or "Missing hostname" in reason or "Malformed" in reason):
            raise InvalidInputUrlError(reason)
        raise UnsafeDestinationError(reason or "Unsafe destination URL")

    return cleaned


def is_direct_amazon_url(url: str) -> bool:
    """
    Return True if `url` is already a direct Amazon marketplace URL (not a shortener
    domain like amzn.to, amzn.eu, a.co, 3rrood.com, etc.).
    """
    if not url or not isinstance(url, str) or not is_http_url(url):
        return False
    try:
        parsed = urlsplit(url.strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower().strip().rstrip(".")
    if not host or host in _SHORTENER_HOSTS:
        return False
    return bool(AMAZON_DOMAINS.search(host))


def extract_merchant_id(url: str) -> str | None:
    """Extract merchant/seller ID from query parameters or raw URL string."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        for key in ("m", "merchant", "seller", "sellerId"):
            if key in params and params[key]:
                val = params[key][0].strip().upper()
                if val:
                    return val
    except Exception:
        pass

    # Regex fallback for embedded query parameters or raw text tokens
    match = re.search(r"[?&](?:m|merchant|seller|sellerId)=([A-Z0-9]+)", url, re.I)
    if match:
        return match.group(1).upper()

    return None


def classify_seller_from_merchant_id(merchant_id: str | None) -> tuple[str, str | None]:
    """
    Classify seller_type and normalize merchant_id based on explicit rules:
    - A2N2MP47XAP1MK -> ("AMAZON_RESALE", "A2N2MP47XAP1MK")
    - A1ZVRGNO5AYLOV -> ("NEW_AMAZON", "A1ZVRGNO5AYLOV")
    - None / Absent  -> ("NEW_AMAZON", None) [no explicit merchant parameter]
    - Unknown 3rd party -> ("NEW_AMAZON", merchant_id) [logged as unsupported 3rd party]
    """
    if not merchant_id:
        logger.debug("SELLER CLASSIFICATION: no explicit merchant_id -> default NEW_AMAZON")
        return ("NEW_AMAZON", None)

    norm_m_id = merchant_id.strip().upper()
    if norm_m_id == AMAZON_RESALE_SELLER_ID:
        logger.debug("SELLER CLASSIFICATION: merchant_id=%s -> AMAZON_RESALE", norm_m_id)
        return ("AMAZON_RESALE", AMAZON_RESALE_SELLER_ID)

    if norm_m_id == NEW_AMAZON_SELLER_ID:
        logger.debug("SELLER CLASSIFICATION: merchant_id=%s -> NEW_AMAZON", norm_m_id)
        return ("NEW_AMAZON", NEW_AMAZON_SELLER_ID)

    logger.warning("SELLER CLASSIFICATION: unknown/unsupported merchant_id=%s -> default NEW_AMAZON", norm_m_id)
    return ("NEW_AMAZON", norm_m_id)


def build_clean_url(asin: str, domain: str, merchant_id: str | None = None) -> str:
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    url = f"https://{domain}/dp/{asin}"
    if merchant_id:
        url = f"{url}?m={merchant_id}"
    return url


def _validate_redirect_history(
    initial_url: str,
    response: Any,
    visited: set[str],
    max_redirects: int = MAX_REDIRECT_HOPS,
) -> str:
    """
    Validate all redirect hops recorded in `response.history` and `response.url`
    for safety, loop detection, and redirect hop limits.
    """
    history = getattr(response, "history", None) or []
    if len(history) > max_redirects:
        raise RedirectLimitExceededError(
            f"Exceeded maximum redirect hops ({len(history)} > {max_redirects})"
        )

    current = initial_url
    for hop_resp in history:
        hop_headers = getattr(hop_resp, "headers", None) or {}
        loc = hop_headers.get("location") if hasattr(hop_headers, "get") else None
        if loc:
            next_hop = urljoin(current, loc.strip())
            safe, reason = check_destination_safety(next_hop)
            if not safe:
                raise UnsafeDestinationError(f"Unsafe redirect hop destination: {reason}")
            if next_hop in visited:
                raise RedirectLoopError(
                    f"Redirect loop detected at {_redact_url_for_log(next_hop)}"
                )
            visited.add(next_hop)
            current = next_hop

    final_url = str(getattr(response, "url", "") or current).strip()
    safe, reason = check_destination_safety(final_url)
    if not safe:
        raise UnsafeDestinationError(f"Unsafe final destination: {reason}")

    if final_url != current and final_url in visited and len(history) > 0:
        raise RedirectLoopError(
            f"Redirect loop detected at {_redact_url_for_log(final_url)}"
        )
    visited.add(final_url)
    return final_url


def _extract_meta_refresh_url(html_text: str, base_url: str) -> str | None:
    """Extract target URL from an HTML `<meta http-equiv="refresh" content="...;url=...">` tag."""
    if not html_text:
        return None
    match = _META_REFRESH_RE.search(html_text[:8192])
    if not match:
        return None
    raw_target = match.group(1).strip().strip("\"'")
    if not raw_target:
        return None
    return urljoin(base_url, raw_target)


async def _resolve_via_browser(url: str, browser_mgr: Any) -> str | None:
    """
    Fallback redirect resolution using the shared Playwright BrowserManager when HTTP
    requests are blocked by anti-bot/Cloudflare protections (e.g. 403 on shorteners).
    """
    if browser_mgr is None or not getattr(browser_mgr, "_browser", None):
        return None

    context = None
    try:
        context = await browser_mgr._browser.new_context(user_agent=USER_AGENT)
        page = await context.new_page()
        response = await page.goto(
            url,
            timeout=int(max(REDIRECT_TIMEOUT_SEC, 15) * 1000),
            wait_until="domcontentloaded",
        )
        # Allow a brief moment if still on the shortener domain for client-side/JS redirect
        current_url = (getattr(page, "url", "") or "").strip()
        if current_url and not is_direct_amazon_url(current_url):
            try:
                await page.wait_for_timeout(1500)
                current_url = (getattr(page, "url", "") or "").strip()
            except Exception:
                pass

        if not current_url:
            return None

        safe, reason = check_destination_safety(current_url)
        if not safe:
            raise UnsafeDestinationError(f"Unsafe browser redirect destination: {reason}")

        status = getattr(response, "status", 200) if response is not None else 200
        if status >= 400 and current_url.rstrip("/") == url.rstrip("/"):
            return None

        return current_url
    except UnsafeDestinationError:
        raise
    except Exception as exc:
        logger.debug("RESOLVER PLAYWRIGHT FALLBACK FAILED url=%s error=%s", _redact_url_for_log(url), exc)
        return None
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass


async def resolve_redirect(url: str) -> str:
    """
    Resolve shortened/redirect URLs to their final destination URL:
    1. Validates and normalizes `url` (enforcing HTTP/HTTPS and destination safety).
    2. Short-circuits direct Amazon marketplace URLs without external network calls.
    3. Follows HTTP redirects hop-by-hop (HEAD with GET fallback), enforcing
       `MAX_REDIRECT_HOPS`, timeout, redirect loop detection, and destination safety.
    4. Falls back to the shared Playwright BrowserManager if HTTP resolution fails
       (e.g. HTTP 403 Cloudflare protection on short-link services like 3rrood.com).
    """
    normalized = normalize_input_url(url)

    if is_direct_amazon_url(normalized):
        _last_resolution_method_ctx.set("direct")
        return normalized

    if _http_client is None:
        await init_http_client()

    assert _http_client is not None

    visited: set[str] = {normalized}
    current_url = normalized
    method_used = "http_head"
    http_error: Exception | None = None
    last_http_status: int | None = None

    for hop in range(MAX_REDIRECT_HOPS + 1):
        if hop > 0 and is_direct_amazon_url(current_url):
            _last_resolution_method_ctx.set(method_used)
            return current_url

        # 1. Try HEAD request first
        head_resp = None
        try:
            try:
                head_resp = await _http_client.head(current_url, follow_redirects=False)
            except TypeError:
                head_resp = await _http_client.head(current_url)
        except httpx.TooManyRedirects as exc:
            raise RedirectLimitExceededError(
                f"Too many redirects while resolving {_redact_url_for_log(normalized)}"
            ) from exc
        except httpx.TimeoutException as exc:
            http_error = RedirectTimeoutError(
                f"Timeout during HEAD request for {_redact_url_for_log(current_url)}: {exc}"
            )
        except httpx.HTTPError as exc:
            http_error = RedirectResolutionError(
                f"HTTP error during HEAD request for {_redact_url_for_log(current_url)}: {exc}"
            )

        if head_resp is not None:
            last_http_status = head_resp.status_code
            headers = getattr(head_resp, "headers", None) or {}
            loc = headers.get("location") if hasattr(headers, "get") else None

            if head_resp.status_code in (301, 302, 303, 307, 308) and loc:
                next_url = urljoin(current_url, loc.strip())
                safe, reason = check_destination_safety(next_url)
                if not safe:
                    raise UnsafeDestinationError(f"Unsafe redirect destination: {reason}")
                if next_url in visited:
                    raise RedirectLoopError(
                        f"Redirect loop detected at {_redact_url_for_log(next_url)}"
                    )
                if hop + 1 > MAX_REDIRECT_HOPS:
                    raise RedirectLimitExceededError(
                        f"Exceeded maximum redirect hops ({MAX_REDIRECT_HOPS})"
                    )
                visited.add(next_url)
                current_url = next_url
                method_used = "http_head"
                continue

            if head_resp.status_code < 400:
                resolved = _validate_redirect_history(
                    current_url, head_resp, visited, max_redirects=MAX_REDIRECT_HOPS - hop
                )
                if resolved != current_url or is_direct_amazon_url(resolved):
                    _last_resolution_method_ctx.set("http_head")
                    logger.debug("RESOLVER SUCCESS (HEAD) final_url=%s", _redact_url_for_log(resolved))
                    return resolved

        # 2. Fallback to GET request (many shorteners block HEAD or use meta-refresh)
        logger.debug("RESOLVER FALLBACK TO GET url=%s", _redact_url_for_log(current_url))
        get_resp = None
        try:
            try:
                get_resp = await _http_client.get(current_url, follow_redirects=False)
            except TypeError:
                get_resp = await _http_client.get(current_url)
        except httpx.TooManyRedirects as exc:
            raise RedirectLimitExceededError(
                f"Too many redirects while resolving {_redact_url_for_log(normalized)}"
            ) from exc
        except httpx.TimeoutException as exc:
            http_error = RedirectTimeoutError(
                f"Timeout during GET request for {_redact_url_for_log(current_url)}: {exc}"
            )
            break
        except httpx.HTTPError as exc:
            http_error = RedirectResolutionError(
                f"HTTP error during GET request for {_redact_url_for_log(current_url)}: {exc}"
            )
            break

        if get_resp is not None:
            last_http_status = get_resp.status_code
            headers = getattr(get_resp, "headers", None) or {}
            loc = headers.get("location") if hasattr(headers, "get") else None

            if get_resp.status_code in (301, 302, 303, 307, 308) and loc:
                next_url = urljoin(current_url, loc.strip())
                safe, reason = check_destination_safety(next_url)
                if not safe:
                    raise UnsafeDestinationError(f"Unsafe redirect destination: {reason}")
                if next_url in visited:
                    raise RedirectLoopError(
                        f"Redirect loop detected at {_redact_url_for_log(next_url)}"
                    )
                if hop + 1 > MAX_REDIRECT_HOPS:
                    raise RedirectLimitExceededError(
                        f"Exceeded maximum redirect hops ({MAX_REDIRECT_HOPS})"
                    )
                visited.add(next_url)
                current_url = next_url
                method_used = "http_get"
                continue

            if get_resp.status_code < 400:
                resolved = _validate_redirect_history(
                    current_url, get_resp, visited, max_redirects=MAX_REDIRECT_HOPS - hop
                )
                if resolved != current_url:
                    _last_resolution_method_ctx.set("http_get")
                    logger.debug("RESOLVER SUCCESS (GET) final_url=%s", _redact_url_for_log(resolved))
                    return resolved

                # Check for HTML meta-refresh redirect
                body_text = getattr(get_resp, "text", "") or ""
                meta_url = _extract_meta_refresh_url(body_text, current_url)
                if meta_url:
                    safe, reason = check_destination_safety(meta_url)
                    if not safe:
                        raise UnsafeDestinationError(f"Unsafe meta-refresh destination: {reason}")
                    if meta_url in visited:
                        raise RedirectLoopError(
                            f"Redirect loop detected via meta-refresh at {_redact_url_for_log(meta_url)}"
                        )
                    if hop + 1 > MAX_REDIRECT_HOPS:
                        raise RedirectLimitExceededError(
                            f"Exceeded maximum redirect hops ({MAX_REDIRECT_HOPS})"
                        )
                    visited.add(meta_url)
                    current_url = meta_url
                    method_used = "http_meta_refresh"
                    continue

                current_url = resolved
                method_used = "http_get"
                break

            # HTTP status >= 400 on GET
            http_error = RedirectResolutionError(
                f"HTTP {get_resp.status_code} when resolving {_redact_url_for_log(current_url)}"
            )
            break

    # 3. Playwright browser fallback when HTTP failed or remained stuck on a shortener
    browser_mgr = _active_browser_ctx.get()
    if browser_mgr is not None and (http_error is not None or not is_direct_amazon_url(current_url)):
        browser_resolved = await _resolve_via_browser(current_url, browser_mgr)
        if browser_resolved and browser_resolved != current_url:
            _last_resolution_method_ctx.set("playwright_browser")
            logger.debug(
                "RESOLVER SUCCESS (PLAYWRIGHT) final_url=%s",
                _redact_url_for_log(browser_resolved),
            )
            return browser_resolved

    if http_error is not None:
        raise http_error

    _last_resolution_method_ctx.set(method_used)
    return current_url


def classify_resolved_url(
    resolved_url: str,
    *,
    original_url: str = "",
    domain: str = "www.amazon.eg",
    resolution_method: str = "direct",
) -> ResolvedDestination:
    """
    Step 3 of Link Resolution Pipeline:
    Classify a resolved URL into:
    - 'promotion' (`promotion_handler`): `/promotion/psp/...`
    - 'search' (`search_handler`): `/s`, `/-/ar/s`, `/-/en/s`, etc.
    - 'product' (`product_handler`): Amazon `/dp/ASIN`, `/gp/product/ASIN`, etc.
    - 'unsupported' (`none`): Any other URL
    Never treats search-query parameters (`k`, `hidden-keywords`) or promotion IDs as product ASINs.
    """
    orig = (original_url or resolved_url or "").strip()
    norm = (resolved_url or "").strip()

    safe, safety_reason = check_destination_safety(norm)
    if not safe:
        return ResolvedDestination(
            original_url=orig,
            normalized_url=norm,
            resolved_url=norm,
            resolution_method=resolution_method,
            destination_type="error",
            selected_handler="none",
            error_reason=f"unsafe_destination: {safety_reason}",
        )

    # 1. Amazon Promotion Page
    if is_promotion_url(norm):
        return ResolvedDestination(
            original_url=orig,
            normalized_url=norm,
            resolved_url=norm,
            resolution_method=resolution_method,
            destination_type="promotion",
            selected_handler="promotion_handler",
        )

    # 2. Amazon Search Results Page
    if is_search_url(norm):
        return ResolvedDestination(
            original_url=orig,
            normalized_url=norm,
            resolved_url=norm,
            resolution_method=resolution_method,
            destination_type="search",
            selected_handler="search_handler",
        )

    # 3. Amazon Product Page
    if is_amazon_url(norm):
        asin = extract_asin(norm)
        if asin:
            asin = asin.upper()
            merchant_id = extract_merchant_id(norm) or (
                extract_merchant_id(orig) if is_amazon_url(orig) else None
            )
            seller_type, resolved_merchant_id = classify_seller_from_merchant_id(merchant_id)
            clean_url = build_clean_url(asin, domain, merchant_id=resolved_merchant_id)
            product_res = ResolvedProductInput(
                asin=asin,
                merchant_id=resolved_merchant_id,
                seller_type=seller_type,
                clean_url=clean_url,
                final_url=norm,
            )
            return ResolvedDestination(
                original_url=orig,
                normalized_url=norm,
                resolved_url=norm,
                resolution_method=resolution_method,
                destination_type="product",
                selected_handler="product_handler",
                asin=asin,
                product_resolution=product_res,
            )

    # 4. Unsupported Destination
    return ResolvedDestination(
        original_url=orig,
        normalized_url=norm,
        resolved_url=norm,
        resolution_method=resolution_method,
        destination_type="unsupported",
        selected_handler="none",
        error_reason=f"unsupported_destination: {_redact_url_for_log(norm)}",
    )


def _log_pipeline_diagnostics(result: ResolvedDestination) -> None:
    """Emit structured diagnostics for the link resolution and classification pipeline."""
    log_fn = logger.info if result.destination_type in ("product", "search", "promotion") else logger.warning
    log_fn(
        "LINK PIPELINE:\n"
        "  original_url=%s\n"
        "  resolved_url=%s\n"
        "  resolution_method=%s\n"
        "  destination_type=%s\n"
        "  selected_handler=%s\n"
        "  failure_reason=%s",
        _redact_url_for_log(result.original_url),
        _redact_url_for_log(result.resolved_url),
        result.resolution_method,
        result.destination_type,
        result.selected_handler,
        result.error_reason or "none",
    )


async def resolve_and_classify_url(
    user_input: str,
    domain: str = "www.amazon.eg",
    *,
    browser: Any = None,
) -> ResolvedDestination:
    """
    Execute the complete Resolve-First, Classify-Second URL pipeline:
    1. Normalize & validate input (or handle standalone 10-char ASIN).
    2. Resolve redirects (Direct Amazon short-circuit -> HTTP HEAD/GET/meta-refresh -> Playwright fallback)
       without requiring an ASIN prior to resolution.
    3. Classify the resolved destination ('product', 'search', 'promotion', or 'unsupported').
    4. Emit structured diagnostics and return `ResolvedDestination`.
    """
    raw = (user_input or "").strip()
    if not raw:
        res = ResolvedDestination(
            original_url="",
            normalized_url="",
            resolved_url=None,
            resolution_method="failed",
            destination_type="error",
            selected_handler="none",
            error_reason="invalid_input_url: empty input",
        )
        _log_pipeline_diagnostics(res)
        return res

    # Support standalone 10-character ASIN inputs (for manual posts / backward compatibility)
    bare_asin = is_standalone_asin(raw)
    if bare_asin:
        seller_type, merchant_id = classify_seller_from_merchant_id(None)
        clean_url = build_clean_url(bare_asin, domain, merchant_id=merchant_id)
        product_res = ResolvedProductInput(
            asin=bare_asin,
            merchant_id=merchant_id,
            seller_type=seller_type,
            clean_url=clean_url,
            final_url=None,
        )
        res = ResolvedDestination(
            original_url=raw,
            normalized_url=clean_url,
            resolved_url=clean_url,
            resolution_method="bare_asin",
            destination_type="product",
            selected_handler="product_handler",
            asin=bare_asin,
            product_resolution=product_res,
        )
        _log_pipeline_diagnostics(res)
        return res

    # Step 1: Normalize and validate input URL
    try:
        normalized_url = normalize_input_url(raw)
    except LinkResolutionError as exc:
        res = ResolvedDestination(
            original_url=raw,
            normalized_url=raw,
            resolved_url=None,
            resolution_method="failed",
            destination_type="error",
            selected_handler="none",
            error_reason=f"{exc.reason_code}: {exc}",
        )
        _log_pipeline_diagnostics(res)
        return res

    # Step 2: Resolve URL (never requires an ASIN prior to resolution)
    resolved_url: str | None = None
    resolution_method = "direct"

    if is_direct_amazon_url(normalized_url):
        resolved_url = normalized_url
        resolution_method = "direct"
    else:
        token_browser = _active_browser_ctx.set(browser)
        token_method = _last_resolution_method_ctx.set("http_redirect")
        try:
            resolved_url = await resolve_redirect(normalized_url)
            resolution_method = _last_resolution_method_ctx.get()
        except LinkResolutionError as exc:
            # Try Playwright fallback if browser was provided and not yet tried
            if (
                browser is not None
                and not isinstance(exc, (UnsafeDestinationError, RedirectLoopError, RedirectLimitExceededError))
            ):
                browser_url = await _resolve_via_browser(normalized_url, browser)
                if browser_url and browser_url != normalized_url:
                    resolved_url = browser_url
                    resolution_method = "playwright_browser"
            if not resolved_url:
                res = ResolvedDestination(
                    original_url=raw,
                    normalized_url=normalized_url,
                    resolved_url=None,
                    resolution_method="failed",
                    destination_type="error",
                    selected_handler="none",
                    error_reason=f"{exc.reason_code}: {exc}",
                )
                _log_pipeline_diagnostics(res)
                return res
        except Exception as exc:
            if browser is not None:
                browser_url = await _resolve_via_browser(normalized_url, browser)
                if browser_url and browser_url != normalized_url:
                    resolved_url = browser_url
                    resolution_method = "playwright_browser"
            if not resolved_url:
                res = ResolvedDestination(
                    original_url=raw,
                    normalized_url=normalized_url,
                    resolved_url=None,
                    resolution_method="failed",
                    destination_type="error",
                    selected_handler="none",
                    error_reason=f"redirect_failed: {exc}",
                )
                _log_pipeline_diagnostics(res)
                return res
        finally:
            _active_browser_ctx.reset(token_browser)
            _last_resolution_method_ctx.reset(token_method)

        # If resolve_redirect was mocked or returned the unchanged non-Amazon shortener URL,
        # attempt Playwright fallback if a browser is available
        if (
            resolved_url
            and browser is not None
            and resolved_url.rstrip("/") == normalized_url.rstrip("/")
            and not is_direct_amazon_url(resolved_url)
        ):
            browser_url = await _resolve_via_browser(normalized_url, browser)
            if browser_url and browser_url != normalized_url:
                resolved_url = browser_url
                resolution_method = "playwright_browser"

    # Step 3: Classify the resolved destination
    classified = classify_resolved_url(
        resolved_url or normalized_url,
        original_url=raw,
        domain=domain,
        resolution_method=resolution_method,
    )
    classified.normalized_url = normalized_url
    _log_pipeline_diagnostics(classified)
    return classified


async def resolve_product_input(
    user_input: str,
    domain: str = "www.amazon.eg",
    *,
    browser: Any = None,
) -> ResolvedProductInput | None:
    """
    Single source of truth for resolving ASIN, merchant_id, seller_type, and clean_url
    for Amazon product inputs.
    Uses the Resolve-First, Classify-Second pipeline (`resolve_and_classify_url`):
    - Returns `ResolvedProductInput` only when the resolved destination is classified as `'product'`.
    - Returns `None` for promotion URLs, search URLs, unsupported destinations, or failed redirects.
    """
    classified = await resolve_and_classify_url(
        user_input,
        domain=domain,
        browser=browser,
    )
    if classified.destination_type == "product" and classified.product_resolution is not None:
        logger.debug(
            "SELLER LIFECYCLE:\n"
            "  stage=RESOLVE_PRODUCT_INPUT\n"
            "  asin=%s\n"
            "  seller_type=%s\n"
            "  merchant_id=%s",
            classified.product_resolution.asin,
            classified.product_resolution.seller_type,
            classified.product_resolution.merchant_id,
        )
        return classified.product_resolution
    return None


async def resolve_asin_from_input(user_input: str) -> str | None:
    """Legacy helper: Returns capitalized 10-char ASIN string or None."""
    res = await resolve_product_input(user_input)
    return res.asin if res else None


