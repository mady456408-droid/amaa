"""Amazon SiteStripe URL Shortener integration.

Converts Amazon affiliate URLs into amzn.to short links using Amazon's SiteStripe API.
"""

import asyncio
import logging
import re
from typing import Final

import httpx

from config import (
    AMAZON_SESSION_ID,
    AMAZON_SESSION_TOKEN,
    AMAZON_UBID_ACBEG,
    AMAZON_AT_ACBEG,
    AMAZON_SESS_AT_ACBEG,
    AMAZON_SHORTENER_ENABLED,
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


async def shorten_amazon_url(
    long_url: str,
    db,
) -> str | None:
    """
    Shorten Amazon affiliate URL using SiteStripe API.

    Args:
        long_url: The Amazon affiliate URL to shorten
        db: Database instance for caching

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
    cached = db.get_shortened_link(long_url)
    if cached:
        logger.info("AMAZON SHORTENER CACHE HIT url=%s short_url=%s", long_url, cached)
        return cached

    # Call Amazon API with exponential retry
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
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    _API_URL,
                    params=params,
                    cookies=cookies,
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()

                if not isinstance(data, dict):
                    logger.warning(
                        "AMAZON SHORTENER FAILED\n"
                        "  reason=invalid_response_not_dict\n"
                        "  attempt=%d/%d\n"
                        "  falling_back_to_original_url=True\n"
                        "  original_url=%s",
                        attempt,
                        max_retries,
                        long_url,
                    )
                    return None

                if not data.get("ok") or not data.get("isOk"):
                    logger.warning(
                        "AMAZON SHORTENER FAILED\n"
                        "  reason=api_rejected\n"
                        "  attempt=%d/%d\n"
                        "  response=%s\n"
                        "  falling_back_to_original_url=True\n"
                        "  original_url=%s",
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
                        "  attempt=%d/%d\n"
                        "  falling_back_to_original_url=True\n"
                        "  original_url=%s",
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

                # Save to cache using affiliate_url as key
                db.save_shortened_link(long_url, short_url)

                return short_url

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            reason_phrase = exc.response.reason_phrase or "Unknown"
            body_preview = exc.response.text[:200] if exc.response.text else ""

            # Retry on 5xx errors, 429 (rate limit), 503 (service unavailable)
            if status_code >= 500 or status_code == 429 or status_code == 503:
                if attempt < max_retries:
                    delay = base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "AMAZON SHORTENER RETRYABLE ERROR\n"
                        "  status=%d\n"
                        "  reason=%s\n"
                        "  attempt=%d/%d\n"
                        "  retrying_in=%.1fs\n"
                        "  body_preview=%s\n"
                        "  original_url=%s",
                        status_code,
                        reason_phrase,
                        attempt,
                        max_retries,
                        delay,
                        body_preview,
                        long_url,
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    logger.warning(
                        "AMAZON SHORTENER FAILED\n"
                        "  status=%d\n"
                        "  reason=%s\n"
                        "  attempt=%d/%d\n"
                        "  falling_back_to_original_url=True\n"
                        "  body_preview=%s\n"
                        "  original_url=%s",
                        status_code,
                        reason_phrase,
                        attempt,
                        max_retries,
                        body_preview,
                        long_url,
                    )
                    return None
            else:
                # Non-retryable 4xx errors (except 429)
                logger.warning(
                    "AMAZON SHORTENER FAILED\n"
                    "  status=%d\n"
                    "  reason=%s\n"
                    "  attempt=%d/%d\n"
                    "  falling_back_to_original_url=True\n"
                    "  body_preview=%s\n"
                    "  original_url=%s",
                    status_code,
                    reason_phrase,
                    attempt,
                    max_retries,
                    body_preview,
                    long_url,
                )
                return None

        except httpx.TimeoutException:
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
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
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                logger.warning(
                    "AMAZON SHORTENER RETRYABLE ERROR\n"
                    "  reason=request_error\n"
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
                    "  reason=request_error\n"
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

        except Exception as exc:
            logger.exception(
                "AMAZON SHORTENER FAILED\n"
                "  reason=unexpected_error\n"
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

    # Should never reach here, but just in case
    logger.warning(
        "AMAZON SHORTENER FAILED\n"
        "  reason=max_retries_exceeded\n"
        "  falling_back_to_original_url=True\n"
        "  original_url=%s",
        long_url,
    )
    return None
