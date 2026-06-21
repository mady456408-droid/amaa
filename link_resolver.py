import logging
import re

import httpx
from telegram import Message
from telegram.constants import MessageEntityType

from config import REDIRECT_TIMEOUT_SEC, USER_AGENT

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


def build_clean_url(asin: str, domain: str) -> str:
    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    return f"https://{domain}/dp/{asin}"


async def resolve_redirect(url: str) -> str:
    """Fast HTTP redirect resolution (no Playwright)."""
    if _http_client is None:
        await init_http_client()

    assert _http_client is not None

    head_status: int | None = None
    try:
        response = await _http_client.head(url)
        head_status = response.status_code
        if head_status < 400:
            final = str(response.url)
            logger.info("RESOLVER SUCCESS final_url=%s", final)
            return final
        logger.info("RESOLVER HEAD FAILED status=%s", head_status)
    except httpx.HTTPError as exc:
        resp = getattr(exc, "response", None)
        head_status = resp.status_code if resp is not None else None
        if head_status is not None:
            logger.info("RESOLVER HEAD FAILED status=%s", head_status)
        else:
            logger.info("RESOLVER HEAD FAILED error=%s", exc)

    # Short-link providers (e.g. a.y-ay.com) often block HEAD — follow redirects via GET.
    logger.info("RESOLVER FALLBACK TO GET")
    response = await _http_client.get(url)
    final = str(response.url)
    logger.info("RESOLVER SUCCESS final_url=%s", final)
    return final
