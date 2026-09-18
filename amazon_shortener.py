"""Amazon SiteStripe URL Shortener integration.

Converts Amazon affiliate URLs into amzn.to short links using Amazon's SiteStripe API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from typing import Any, Final

import httpx

from config import (
    AMAZON_AT_ACBEG,
    AMAZON_SESSION_ID,
    AMAZON_SESSION_TOKEN,
    AMAZON_SESS_AT_ACBEG,
    AMAZON_SHORTENER_ENABLED,
    AMAZON_UBID_ACBEG,
)

logger = logging.getLogger(__name__)

_API_URL: Final[str] = "https://www.amazon.eg/associates/sitestripe/getShortUrl"
_MARKETPLACE_ID: Final[str] = "623225021"
_USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/149.0.0.0 Safari/537.36"
)

_ASIN_RE: Final[re.Pattern[str]] = re.compile(
    r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)",
    re.IGNORECASE,
)


def extract_asin_from_url(url: str) -> str | None:
    """Extract ASIN from Amazon URL."""
    match = _ASIN_RE.search(url)
    return match.group(1).upper() if match else None


def _format_body_preview(text: str, max_chars: int = 1000) -> str:
    """Format response body text safely for diagnostic logs."""
    if not text:
        return "<EMPTY_BODY>"
    cleaned = text.strip()
    if len(cleaned) > max_chars:
        return f"{cleaned[:max_chars]}... [truncated {len(cleaned)} bytes]"
    return cleaned


def _extract_retry_after(headers: httpx.Headers) -> float | None:
    """Extract Retry-After header as float seconds if available."""
    val = headers.get("retry-after") or headers.get("Retry-After")
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


async def shorten_amazon_url(
    long_url: str,
    db: Any,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """
    Shorten Amazon affiliate URL using SiteStripe API.

    Args:
        long_url: The Amazon affiliate URL to shorten
        db: Database instance for caching
        client: Optional httpx.AsyncClient for dependency injection during testing

    Returns:
        Shortened amzn.to URL if successful, None otherwise
        Note: Returns None on any failure - caller must fall back to original URL
    """
    if not AMAZON_SHORTENER_ENABLED:
        logger.info("AMAZON SHORTENER DISABLED")
        return None

    if not long_url:
        logger.warning(
            "AMAZON SHORTENER FAILED\n"
            "  reason=empty_url\n"
            "  falling_back_to_original_url=True\n"
            "  original_url=%s",
            long_url,
        )
        return None

    # Check required cookies
    if not all([
        AMAZON_SESSION_ID,
        AMAZON_SESSION_TOKEN,
        AMAZON_UBID_ACBEG,
        AMAZON_AT_ACBEG,
        AMAZON_SESS_AT_ACBEG,
    ]):
        logger.warning(
            "AMAZON SHORTENER FAILED\n"
            "  reason=missing_required_cookies\n"
            "  falling_back_to_original_url=True\n"
            "  original_url=%s",
            long_url,
        )
        return None

    # Check cache first using affiliate_url as key
    if db is not None and hasattr(db, "get_shortened_link"):
        cached = db.get_shortened_link(long_url)
        if cached:
            logger.info("AMAZON SHORTENER CACHE HIT url=%s short_url=%s", long_url, cached)
            return cached

    logger.info("AMAZON SHORTENER REQUEST url=%s", long_url)

    cookies = {
        "session-id": AMAZON_SESSION_ID,
        "session-token": AMAZON_SESSION_TOKEN,
        "ubid-acbeg": AMAZON_UBID_ACBEG,
        "at-acbeg": AMAZON_AT_ACBEG,
        "sess-at-acbeg": AMAZON_SESS_AT_ACBEG,
    }

    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.amazon.eg/",
    }

    params = {
        "longUrl": long_url,
        "marketplaceId": _MARKETPLACE_ID,
        "storeId": "ahmedhamedmoh-21",
    }

    max_retries = 3
    base_delay = 1.0  # seconds

    for attempt in range(1, max_retries + 1):
        close_client = False
        active_client = client
        if active_client is None:
            active_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
            close_client = True

        try:
            response = await active_client.get(
                _API_URL,
                params=params,
                cookies=cookies,
                headers=headers,
            )
        except httpx.TimeoutException:
            if close_client and active_client:
                await active_client.aclose()
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                logger.warning(
                    "AMAZON SHORTENER RETRYABLE ERROR\n"
                    "  reason=timeout\n"
                    "  attempt=%d/%d\n"
                    "  retrying_in=%.1fs\n"
                    "  original_url=%s",
                    attempt,
                    max_retries,
                    delay,
                    long_url,
                )
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=timeout\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    attempt,
                    max_retries,
                    long_url,
                )
                return None

        except httpx.RequestError as exc:
            if close_client and active_client:
                await active_client.aclose()
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                logger.warning(
                    "AMAZON SHORTENER RETRYABLE ERROR\n"
                    "  reason=network_error\n"
                    "  error=%s\n"
                    "  attempt=%d/%d\n"
                    "  retrying_in=%.1fs\n"
                    "  original_url=%s",
                    str(exc),
                    attempt,
                    max_retries,
                    delay,
                    long_url,
                )
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=network_error\n"
                    "  error=%s\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    str(exc),
                    attempt,
                    max_retries,
                    long_url,
                )
                return None
        finally:
            if close_client and active_client:
                await active_client.aclose()

        status_code = response.status_code
        content_type = response.headers.get("content-type", "unknown")
        body_preview = _format_body_preview(response.text)
        request_url = str(response.url)

        # Always log diagnostic response details
        logger.info(
            "AMAZON SHORTENER RESPONSE\n"
            "  status=%d\n"
            "  content_type=%s\n"
            "  request_url=%s\n"
            "  attempt=%d/%d\n"
            "  body_preview=%s",
            status_code,
            content_type,
            request_url,
            attempt,
            max_retries,
            body_preview,
        )

        # 1. Auth Failures (401 / 403) -> No retries
        if status_code in (401, 403):
            logger.warning(
                "AMAZON SHORTENER FAILED\n"
                "  reason=auth_failure\n"
                "  status=%d\n"
                "  content_type=%s\n"
                "  attempt=%d/%d\n"
                "  falling_back_to_original_url=True\n"
                "  original_url=%s",
                status_code,
                content_type,
                attempt,
                max_retries,
                long_url,
            )
            return None

        # 2. Rate Limited (429) -> Respect Retry-After header or backoff
        if status_code == 429:
            retry_after = _extract_retry_after(response.headers)
            delay = retry_after if retry_after is not None else base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
            if attempt < max_retries:
                logger.warning(
                    "AMAZON SHORTENER RETRYABLE ERROR\n"
                    "  reason=rate_limited\n"
                    "  status=429\n"
                    "  retry_after=%.1f\n"
                    "  attempt=%d/%d\n"
                    "  retrying_in=%.1fs\n"
                    "  original_url=%s",
                    retry_after or 0.0,
                    attempt,
                    max_retries,
                    delay,
                    long_url,
                )
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=rate_limited\n"
                    "  status=429\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    attempt,
                    max_retries,
                    long_url,
                )
                return None

        # 3. Server Error (5xx) -> Bounded backoff retry
        if status_code >= 500:
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0.0, 0.5)
                logger.warning(
                    "AMAZON SHORTENER RETRYABLE ERROR\n"
                    "  reason=server_error\n"
                    "  status=%d\n"
                    "  attempt=%d/%d\n"
                    "  retrying_in=%.1fs\n"
                    "  original_url=%s",
                    status_code,
                    attempt,
                    max_retries,
                    delay,
                    long_url,
                )
                await asyncio.sleep(delay)
                continue
            else:
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=server_error\n"
                    "  status=%d\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    status_code,
                    attempt,
                    max_retries,
                    long_url,
                )
                return None

        # 4. Other 4xx Errors (e.g. 400, 404, 405) -> No retries
        if 400 <= status_code < 500:
            logger.warning(
                "AMAZON SHORTENER FAILED\n"
                "  reason=client_error\n"
                "  status=%d\n"
                "  attempt=%d/%d\n"
                "  falling_back_to_original_url=True\n"
                "  original_url=%s",
                status_code,
                attempt,
                max_retries,
                long_url,
            )
            return None

        # 5. Success Status (HTTP 2xx) -> Parse JSON safely
        if 200 <= status_code < 300:
            try:
                data = response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=non_json_response\n"
                    "  status=%d\n"
                    "  content_type=%s\n"
                    "  attempt=%d/%d\n"
                    "  error=%s\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    status_code,
                    content_type,
                    attempt,
                    max_retries,
                    str(exc),
                    long_url,
                )
                return None

            if not isinstance(data, dict):
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=invalid_response_not_dict\n"
                    "  status=%d\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    status_code,
                    attempt,
                    max_retries,
                    long_url,
                )
                return None

            if not data.get("ok") and not data.get("isOk") and not data.get("shortUrl"):
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=api_rejected\n"
                    "  status=%d\n"
                    "  attempt=%d/%d\n"
                    "  response=%s\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    status_code,
                    attempt,
                    max_retries,
                    data,
                    long_url,
                )
                return None

            short_url = data.get("shortUrl")
            if not short_url or not isinstance(short_url, str):
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  reason=missing_or_invalid_short_url\n"
                    "  status=%d\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  original_url=%s",
                    status_code,
                    attempt,
                    max_retries,
                    long_url,
                )
                return None

            logger.info(
                "AMAZON SHORTENER SUCCESS\n"
                "  attempt=%d/%d\n"
                "  short_url=%s\n"
                "  original_url=%s",
                attempt,
                max_retries,
                short_url,
                long_url,
            )

            if db is not None and hasattr(db, "save_shortened_link"):
                db.save_shortened_link(long_url, short_url)

            return short_url

    logger.warning(
        "AMAZON SHORTENER FAILED\n"
        "  reason=max_retries_exceeded\n"
        "  falling_back_to_original_url=True\n"
        "  original_url=%s",
        long_url,
    )
    return None
