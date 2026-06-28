"""Amazon SiteStripe URL Shortener integration.

Converts Amazon affiliate URLs into amzn.to short links using Amazon's SiteStripe API.
"""

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
    """
    if not AMAZON_SHORTENER_ENABLED:
        logger.info("AMAZON SHORTENER DISABLED")
        return None

    if not long_url:
        logger.warning("AMAZON SHORTENER FAILED: empty URL")
        return None

    # Check required cookies
    if not all([
        AMAZON_SESSION_ID,
        AMAZON_SESSION_TOKEN,
        AMAZON_UBID_ACBEG,
        AMAZON_AT_ACBEG,
        AMAZON_SESS_AT_ACBEG,
    ]):
        logger.warning("AMAZON SHORTENER FAILED: missing required cookies")
        return None

    # Check cache first using affiliate_url as key
    cached = db.get_shortened_link(long_url)
    if cached:
        logger.info("AMAZON SHORTENER CACHE HIT url=%s short_url=%s", long_url, cached)
        return cached

    # Call Amazon API
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
                logger.warning("AMAZON SHORTENER INVALID RESPONSE: not a dict")
                return None

            if not data.get("ok") or not data.get("isOk"):
                logger.warning(
                    "AMAZON SHORTENER API REJECTED response=%s",
                    data,
                )
                return None

            short_url = data.get("shortUrl")
            if not short_url or not isinstance(short_url, str):
                logger.warning("AMAZON SHORTENER INVALID RESPONSE: missing or invalid shortUrl")
                return None

            logger.info("AMAZON SHORTENER SUCCESS short_url=%s", short_url)

            # Save to cache using affiliate_url as key
            db.save_shortened_link(long_url, short_url)

            return short_url

    except httpx.HTTPStatusError as exc:
        logger.warning(
            "AMAZON SHORTENER FAILED status=%s body=%s",
            exc.response.status_code,
            exc.response.text[:500],
        )
        return None
    except httpx.TimeoutException:
        logger.warning("AMAZON SHORTENER FAILED: timeout")
        return None
    except httpx.RequestError as exc:
        logger.warning("AMAZON SHORTENER FAILED: request error %s", exc)
        return None
    except Exception as exc:
        logger.exception("AMAZON SHORTENER FAILED: unexpected error")
        return None
