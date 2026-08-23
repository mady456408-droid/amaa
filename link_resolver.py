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


from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

NEW_AMAZON_SELLER_ID = "A1ZVRGNO5AYLOV"
AMAZON_RESALE_SELLER_ID = "A2N2MP47XAP1MK"


@dataclass
class ResolvedProductInput:
    asin: str
    merchant_id: str | None
    seller_type: str  # 'AMAZON_RESALE' or 'NEW_AMAZON'
    clean_url: str
    final_url: str | None = None


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
            logger.debug("RESOLVER SUCCESS final_url=%s", final)
            return final
        logger.debug("RESOLVER HEAD FAILED status=%s", head_status)
    except httpx.HTTPError as exc:
        resp = getattr(exc, "response", None)
        head_status = resp.status_code if resp is not None else None
        if head_status is not None:
            logger.debug("RESOLVER HEAD FAILED status=%s", head_status)
        else:
            logger.debug("RESOLVER HEAD FAILED error=%s", exc)

    # Short-link providers (e.g. a.y-ay.com) often block HEAD — follow redirects via GET.
    logger.debug("RESOLVER FALLBACK TO GET")
    response = await _http_client.get(url)
    final = str(response.url)
    logger.debug("RESOLVER SUCCESS final_url=%s", final)
    return final


async def resolve_product_input(user_input: str, domain: str = "www.amazon.eg") -> ResolvedProductInput | None:
    """
    Single source of truth for resolving ASIN, merchant_id, seller_type, and clean_url.
    Preserves merchant parameters across HTTP redirects (e.g. short links amzn.to / a.co).
    """
    if not user_input:
        return None
    text = user_input.strip()
    if not text:
        return None

    logger.debug("PRODUCT RESOLVER INPUT: %s", text)

    # 1. Direct 10-char ASIN check
    if len(text) == 10 and re.match(r"^[A-Z0-9]{10}$", text, re.I):
        asin = text.upper()
        seller_type, merchant_id = classify_seller_from_merchant_id(None)
        clean_url = build_clean_url(asin, domain, merchant_id=merchant_id)
        logger.debug(
            "RESOLVER RESULT (bare ASIN):\n"
            "  asin=%s\n"
            "  seller_type=%s\n"
            "  merchant_id=%s\n"
            "  clean_url=%s",
            asin,
            seller_type,
            merchant_id,
            clean_url,
        )
        return ResolvedProductInput(
            asin=asin,
            merchant_id=merchant_id,
            seller_type=seller_type,
            clean_url=clean_url,
            final_url=None,
        )

    # 2. Direct Amazon URL or HTTP short link redirect
    final_url: str | None = None
    if is_http_url(text):
        try:
            final_url = await resolve_redirect(text)
        except Exception as exc:
            logger.warning("RESOLVER REDIRECT FAILED input=%s exc=%s", text, exc)

    target_url = final_url or text
    asin = extract_asin(target_url) or extract_asin(text)

    if not asin:
        match = re.search(r"\b([A-Z0-9]{10})\b", text, re.I)
        if match:
            token = match.group(1).upper()
            if len(token) == 10:
                asin = token

    if not asin:
        logger.warning("RESOLVER FAILED: no valid ASIN found in input=%r", text)
        return None

    asin = asin.upper()
    merchant_id = extract_merchant_id(text) or extract_merchant_id(target_url)
    seller_type, resolved_merchant_id = classify_seller_from_merchant_id(merchant_id)
    clean_url = build_clean_url(asin, domain, merchant_id=resolved_merchant_id)

    logger.info(
        "SELLER LIFECYCLE:\n"
        "  stage=RESOLVE_PRODUCT_INPUT\n"
        "  asin=%s\n"
        "  seller_type=%s\n"
        "  merchant_id=%s",
        asin,
        seller_type,
        resolved_merchant_id,
    )

    return ResolvedProductInput(
        asin=asin,
        merchant_id=resolved_merchant_id,
        seller_type=seller_type,
        clean_url=clean_url,
        final_url=final_url,
    )


async def resolve_asin_from_input(user_input: str) -> str | None:
    """Legacy helper: Returns capitalized 10-char ASIN string or None."""
    res = await resolve_product_input(user_input)
    return res.asin if res else None

