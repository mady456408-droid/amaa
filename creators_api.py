"""
Amazon Creators API client — primary product data source.

OAuth 2.0 token management, rate limiting, resource profiles, and response
normalization. Playwright remains the fallback path in product_fetcher.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from amazon_image_url import pick_best_primary_image_url
from coupon_price import parse_price_number
from config import (
    CREATORS_API_TPD,
    CREATORS_API_TPS,
    CREATORS_CREDENTIAL_ID,
    CREATORS_CREDENTIAL_SECRET,
    CREATORS_CREDENTIAL_VERSION,
    CREATORS_MARKETPLACE,
    CREATORS_PARTNER_TAG,
    CREATORS_TOKEN_REFRESH_MARGIN_SEC,
)

logger = logging.getLogger(__name__)

CATALOG_BASE = "https://creatorsapi.amazon/catalog/v1"

# Amazon locale reference for www.amazon.eg — Arabic is ar_AE (not ar_EG).
_EGYPT_ARABIC_LANGUAGE = "ar_AE"

# Reusable resource profiles (request only what is needed).
DRAFT_PROFILE: list[str] = [
    "itemInfo.title",
    "images.primary.large",
    "images.primary.medium",
    "offersV2.listings.price",
    "offersV2.listings.dealDetails",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.condition",
]

PRICE_DROP_PROFILE: list[str] = [
    "offersV2.listings.price",
    "offersV2.listings.dealDetails",
    "offersV2.listings.merchantInfo",
    "offersV2.listings.condition",
]

SEARCH_PROFILE: list[str] = [
    "itemInfo.title",
    "images.primary.medium",
]

FEATURES_PROFILE: list[str] = [
    "itemInfo.features",
]

PROFILE_TTL_SECONDS: dict[str, int] = {
    "draft": 3600,          # offers refresh hourly
    "price_drop": 3600,
    "search": 86400,
    "features": 86400,
}

_TOKEN_ENDPOINTS_V2 = {
    "2.1": "https://creatorsapi.auth.us-east-1.amazoncognito.com/oauth2/token",
    "2.2": "https://creatorsapi.auth.eu-south-2.amazoncognito.com/oauth2/token",
    "2.3": "https://creatorsapi.auth.us-west-2.amazoncognito.com/oauth2/token",
}

_TOKEN_ENDPOINT_V3 = "https://api.amazon.com/auth/o2/token"
_SCOPE_V2 = "creatorsapi/default"
_SCOPE_V3 = "creatorsapi::default"


# Max chars of response body attached to fallback logs / error diagnostics.
_RESPONSE_BODY_LOG_LIMIT = 500


class CreatorsAPIError(Exception):
    """Creators API request failed."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_body: str | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body
        self.retry_after = retry_after


def _mask_partner_tag(tag: str) -> str:
    """Partially mask partner tag for safe startup logs (e.g. loq****-21)."""
    tag = (tag or "").strip()
    if not tag:
        return "****"
    if "-" in tag:
        prefix, suffix = tag.rsplit("-", 1)
        visible = prefix[:3] if len(prefix) >= 3 else prefix[:1]
        return f"{visible}****-{suffix}"
    if len(tag) <= 4:
        return "****"
    return f"{tag[:3]}****"


def _languages_of_preference(marketplace: str) -> list[str] | None:
    """Return Creators API languagesOfPreference for localized titles, if applicable."""
    normalized = (marketplace or "").strip().lower().rstrip("/")
    if normalized == "www.amazon.eg":
        return [_EGYPT_ARABIC_LANGUAGE]
    return None


def _log_creators_request(
    *,
    version: str,
    marketplace: str,
    partner_tag: str,
    item_ids: list[str],
    resources: list[str],
    languages_of_preference: list[str] | None = None,
) -> None:
    """Log GetItems payload metadata — never log secrets or tokens."""
    logger.info(
        "CREATORS REQUEST:\n"
        "version=v%s\n"
        "marketplace=%s\n"
        "partner_tag=%s\n"
        "languages_of_preference=%r\n"
        "item_count=%s\n"
        "item_ids=%r\n"
        "resources=%r",
        version,
        marketplace,
        partner_tag,
        languages_of_preference,
        len(item_ids),
        item_ids,
        resources,
    )


def _log_creators_headers(*, marketplace: str) -> None:
    """Log sanitized outbound headers — Authorization value is always masked."""
    logger.info(
        "CREATORS HEADERS:\n"
        "Content-Type=application/json\n"
        "Authorization=Bearer ****\n"
        "x-marketplace=%s",
        marketplace,
    )


def _parse_response_json(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _log_creators_response(resp: httpx.Response) -> dict[str, Any] | None:
    """Log HTTP response details for GetItems diagnostics."""
    text = resp.text or ""
    parsed = _parse_response_json(text)
    header_dict = dict(resp.headers)
    # Full body on errors (403 diagnosis); truncate large success payloads.
    logged_text = text if resp.status_code >= 400 else text[:_RESPONSE_BODY_LOG_LIMIT]

    logger.info(
        "CREATORS RESPONSE:\n"
        "status=%s\n"
        "headers=%r\n"
        "text=%r",
        resp.status_code,
        header_dict,
        logged_text,
    )

    if parsed is not None:
        if resp.status_code >= 400:
            logger.debug("CREATORS RESPONSE JSON:\n%r", parsed)
        else:
            logger.debug("CREATORS RESPONSE JSON: parsed_ok keys=%r", list(parsed.keys()))
    else:
        logger.debug("CREATORS RESPONSE JSON: unavailable")

    if resp.status_code == 403:
        _log_creators_403_diagnosis(text, parsed)

    return parsed


def _log_creators_403_diagnosis(text: str, parsed: dict[str, Any] | None) -> None:
    """Heuristic classification of 403 responses — observability only."""
    haystack = text.lower()
    if parsed:
        haystack += " " + json.dumps(parsed, ensure_ascii=False).lower()

    if any(
        needle in haystack
        for needle in (
            "unauthorizedpartnertag",
            "unauthorized partner tag",
            "invalidpartnertag",
            "invalid partner tag",
            "partner tag",
            "partnertag",
        )
    ):
        cause = "Unauthorized Partner Tag"
    elif any(
        needle in haystack
        for needle in (
            "marketplace mismatch",
            "invalidmarketplace",
            "invalid marketplace",
            "unsupported marketplace",
            "marketplace not",
        )
    ):
        cause = "Marketplace mismatch"
    elif any(
        needle in haystack
        for needle in (
            "accessdenied",
            "access denied",
            "not approved",
            "not enabled",
            "pending approval",
            "creators api access",
        )
    ):
        cause = "Creators API access not approved"
    else:
        cause = "Unknown 403 reason"

    logger.info("CREATORS DIAGNOSIS:\nPossible cause: %s", cause)


def creators_api_configured() -> bool:
    """True when minimum Creators API credentials are present."""
    return bool(
        CREATORS_CREDENTIAL_ID
        and CREATORS_CREDENTIAL_SECRET
        and CREATORS_CREDENTIAL_VERSION
        and CREATORS_PARTNER_TAG
        and CREATORS_MARKETPLACE
    )


def _token_endpoint(version: str) -> str:
    if version in _TOKEN_ENDPOINTS_V2:
        return _TOKEN_ENDPOINTS_V2[version]
    if version.startswith("3."):
        return _TOKEN_ENDPOINT_V3
    raise CreatorsAPIError(f"Unsupported credential version: {version}")


def _token_scope(version: str) -> str:
    if version.startswith("2."):
        return _SCOPE_V2
    if version.startswith("3."):
        return _SCOPE_V3
    raise CreatorsAPIError(f"Unsupported credential version: {version}")


def _auth_header(token: str, version: str) -> str:
    if version.startswith("3."):
        return f"Bearer {token}"
    return f"Bearer {token}, Version {version}"


@dataclass
class NormalizedItem:
    """Normalized Creators API item for bot consumption."""

    asin: str
    title: str
    price: str
    image_url: str | None
    features: list[str]
    detail_page_url: str
    list_price: str | None = None
    prime_exclusive: bool = False
    seller_name: str | None = None
    raw_listings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "asin": self.asin,
            "title": self.title,
            "price": self.price,
            "image_url": self.image_url,
            "features": self.features,
            "detail_page_url": self.detail_page_url,
            "list_price": self.list_price,
            "prime_exclusive": self.prime_exclusive,
            "seller_name": self.seller_name,
            "raw_listings": self.raw_listings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NormalizedItem:
        return cls(
            asin=str(data.get("asin") or "").upper(),
            title=str(data.get("title") or "Not found"),
            price=str(data.get("price") or "Not found"),
            image_url=data.get("image_url"),
            features=list(data.get("features") or []),
            detail_page_url=str(data.get("detail_page_url") or ""),
            list_price=data.get("list_price"),
            prime_exclusive=bool(data.get("prime_exclusive")),
            seller_name=data.get("seller_name"),
            raw_listings=list(data.get("raw_listings") or []),
        )


class TokenManager:
    """OAuth 2.0 token cache — one token reused across all requests."""

    def __init__(
        self,
        credential_id: str,
        credential_secret: str,
        version: str,
        *,
        refresh_margin_sec: int = CREATORS_TOKEN_REFRESH_MARGIN_SEC,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._credential_id = credential_id
        self._credential_secret = credential_secret
        self._version = version
        self._refresh_margin = refresh_margin_sec
        self._http = http_client
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    async def get_token(self) -> str:
        async with self._lock:
            now = time.monotonic()
            if self._token and now < self._expires_at:
                return self._token
            token, expires_in = await self._fetch_token()
            self._token = token
            margin = min(self._refresh_margin, max(0, expires_in - 60))
            self._expires_at = now + expires_in - margin
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
            logger.info(
                "CREATORS TOKEN REFRESH\n"
                "version=v%s\n"
                "expires_in=%s\n"
                "expires_at=%s",
                self._version,
                expires_in,
                expires_at.isoformat(),
            )
            return self._token

    async def _fetch_token(self) -> tuple[str, int]:
        endpoint = _token_endpoint(self._version)
        scope = _token_scope(self._version)
        client = self._http or httpx.AsyncClient(timeout=30.0)

        if self._version.startswith("3."):
            form = {
                "grant_type": "client_credentials",
                "scope": scope,
            }
            auth = base64.b64encode(
                f"{self._credential_id}:{self._credential_secret}".encode()
            ).decode()
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {auth}",
            }
        else:
            form = {
                "grant_type": "client_credentials",
                "client_id": self._credential_id,
                "client_secret": self._credential_secret,
                "scope": scope,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            resp = await client.post(
                endpoint,
                content=urlencode(form),
                headers=headers,
            )
        finally:
            if self._http is None:
                await client.aclose()

        if resp.status_code >= 400:
            raise CreatorsAPIError(
                f"Token request failed: HTTP {resp.status_code}",
                status_code=resp.status_code,
            )

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise CreatorsAPIError("Token response missing access_token")
        expires_in = int(data.get("expires_in") or 3600)
        return token, expires_in


class CreatorsRateLimiter:
    """Asyncio-safe limiter: TPS + daily quota + global cooldown."""

    def __init__(
        self,
        name: str = "REALTIME",
        *,
        tps: float = CREATORS_API_TPS,
        tpd: int = CREATORS_API_TPD,
    ):
        self.name = name
        self._min_interval = 1.0 / tps if tps > 0 else 0.0
        self._tpd = tpd
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._day_key = ""
        self._day_count = 0
        self._queue_depth = 0
        self._active_requests = 0
        self._cooldown_until = 0.0
        self._consecutive_429 = 0

    async def acquire(self, source: str = "REALTIME") -> None:
        wait = 0.0
        async with self._lock:
            self._queue_depth += 1
            curr_queue = self._queue_depth

            day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if day_key != self._day_key:
                self._day_key = day_key
                self._day_count = 0

            if self._day_count >= self._tpd:
                self._queue_depth -= 1
                raise CreatorsAPIError(
                    "Daily Creators API quota exceeded",
                    status_code=429,
                )

            now = time.monotonic()
            base_time = max(now, self._cooldown_until)
            if self._last_request < base_time:
                target_time = base_time
            else:
                target_time = self._last_request + self._min_interval

            wait = max(0.0, target_time - now)
            self._last_request = target_time
            self._day_count += 1
            self._active_requests += 1

            cooldown_rem = max(0.0, self._cooldown_until - now)
            logger.info(
                "CREATORS RATE LIMIT: name=%s source=%s wait_ms=%.1f active_requests=%s queue_depth=%s cooldown_remaining_ms=%.1f",
                self.name,
                source,
                wait * 1000.0,
                self._active_requests,
                curr_queue,
                cooldown_rem * 1000.0,
            )

        # Sleep OUTSIDE the lock to prevent blocking callers
        if wait > 0:
            await asyncio.sleep(wait)

        async with self._lock:
            self._queue_depth = max(0, self._queue_depth - 1)

    async def release_request(self) -> None:
        async with self._lock:
            self._active_requests = max(0, self._active_requests - 1)

    async def record_cooldown(self, duration: float) -> None:
        """Set or extend global cooldown for this limiter."""
        async with self._lock:
            now = time.monotonic()
            target = now + max(0.0, duration)
            if target > self._cooldown_until:
                self._cooldown_until = target
                logger.info(
                    "CREATORS RATE LIMIT COOLDOWN: name=%s duration=%.2fs cooldown_until=%.2f",
                    self.name,
                    duration,
                    self._cooldown_until,
                )

    async def record_success(self) -> None:
        """Reset consecutive 429 counter on success."""
        async with self._lock:
            self._consecutive_429 = 0

    def get_cooldown_remaining(self) -> float:
        now = time.monotonic()
        return max(0.0, self._cooldown_until - now)


def _format_egp_price(money: dict[str, Any] | None) -> str:
    """Format API money object as Egyptian Pounds display string."""
    if not money:
        return "Not found"
    amount = money.get("amount")
    if amount is not None:
        try:
            val = float(amount)
            if val > 0:
                if abs(val - round(val)) < 0.01:
                    return f"{int(round(val))} جنيه"
                return f"{val:.2f} جنيه"
        except (TypeError, ValueError):
            pass
    display = (money.get("displayAmount") or "").strip()
    if display:
        val = parse_price_number(display)
        if val and val > 0:
            if abs(val - round(val)) < 0.01:
                return f"{int(round(val))} جنيه"
            return f"{val:.2f} جنيه"
        if "جنيه" in display or "EGP" in display.upper():
            return display
        return f"{display} جنيه"
    return "Not found"


NEW_AMAZON_SELLER_ID = "A1ZVRGNO5AYLOV"
AMAZON_RESALE_SELLER_ID = "A2N2MP47XAP1MK"


def _parse_price_val(text: str | None) -> float | None:
    if not text:
        return None
    return parse_price_number(text)


def _extract_offer_condition(listing: dict[str, Any]) -> str | None:
    cond = listing.get("condition")
    if not cond:
        return None
    if isinstance(cond, str):
        return cond.strip()
    if isinstance(cond, dict):
        sub = cond.get("subCondition")
        sub_val = None
        if isinstance(sub, dict):
            sub_val = sub.get("displayValue") or sub.get("value")
        elif isinstance(sub, str):
            sub_val = sub

        main_val = cond.get("displayValue") or cond.get("value") or ""
        
        if sub_val and main_val:
            main_norm = main_val.replace(" ", "").replace("-", "").lower()
            sub_norm = sub_val.replace(" ", "").replace("-", "").lower()
            if sub_norm in main_norm:
                return main_val
            return f"{main_val} - {sub_val}"
        return main_val or sub_val or None
    return None


def log_resale_offer_evidence(
    *,
    asin: str,
    source: str,  # 'LIVE_API' or 'CACHE'
    offer_found: bool,
    price: str | None,
    availability: str,
    raw_listing_count: int,
    module: str = "creators_api",
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info(
        "RESALE OFFER EVIDENCE:\n"
        "  asin=%s\n"
        "  merchant_id=A2N2MP47XAP1MK\n"
        "  source=%s\n"
        "  offer_found=%s\n"
        "  price=%s\n"
        "  availability=%s\n"
        "  timestamp=%s\n"
        "  raw_listing_count=%d",
        asin.upper(),
        source,
        offer_found,
        price or "None",
        availability,
        now_iso,
        raw_listing_count,
    )


def extract_seller_offer(
    item: Any | None,
    seller_type: str,
) -> tuple[str, str | None, float | None, str | None, float | None, str | None, str | None]:
    """
    Extract seller offer details for seller_type ('NEW_AMAZON' or 'AMAZON_RESALE').
    Returns tuple: (status, price_text, price_value, list_price_text, list_price_value, seller_name, seller_condition)
    status: 'AVAILABLE', 'OUT_OF_STOCK', 'MISSING_MERCHANT', or 'UNKNOWN' (API failure)
    """
    if not item:
        return ("UNKNOWN", None, None, None, None, None, None)

    target_merchant_id = NEW_AMAZON_SELLER_ID if seller_type == "NEW_AMAZON" else AMAZON_RESALE_SELLER_ID

    if isinstance(item, dict) and seller_type in item:
        data = item[seller_type]
        m_id = (data.get("merchant_id") or "").strip().upper()
        avail = data.get("availability") or "IN_STOCK"
        if m_id == target_merchant_id:
            if avail == "OUT_OF_STOCK" or data.get("price") is None:
                return ("OUT_OF_STOCK", None, None, None, None, None, None)
            val = float(data["price"])
            p_text = f"{val:.2f} EGP"
            s_name = data.get("merchant_name") or ("Amazon.eg" if seller_type == "NEW_AMAZON" else "Amazon Resale")
            s_cond = data.get("condition") or data.get("seller_condition")
            return ("AVAILABLE", p_text, val, None, None, s_name, s_cond)
        return ("MISSING_MERCHANT", None, None, None, None, None, None)

    if isinstance(item, dict):
        listings = item.get("raw_listings") or []
    else:
        listings = getattr(item, "raw_listings", []) or []

    merchant_found = False
    matching_offers = []

    for listing in listings:
        merchant_info = (
            listing.get("merchantInfo")
            or listing.get("merchant")
            or listing.get("seller")
            or {}
        )
        m_id = (
            merchant_info.get("id")
            or merchant_info.get("merchantId")
            or merchant_info.get("sellerId")
            or merchant_info.get("merchant_id")
            or merchant_info.get("seller_id")
            or listing.get("merchantId")
            or listing.get("sellerId")
            or listing.get("merchant_id")
            or listing.get("seller_id")
            or ""
        ).strip().upper()

        if not m_id and target_merchant_id in str(listing).upper():
            m_id = target_merchant_id

        m_name = merchant_info.get("name") or merchant_info.get("displayName") or ""
        cond = listing.get("condition") or {}
        price_obj = listing.get("price") or {}
        is_buybox = listing.get("isBuyBoxWinner")

        logger.debug(
            "MERCHANT INSPECTION: m_info=%r m_id=%r (target=%r match=%s) name=%r cond=%r price=%r is_buybox=%s",
            merchant_info,
            m_id,
            target_merchant_id,
            m_id == target_merchant_id,
            m_name,
            cond,
            price_obj,
            is_buybox,
        )

        if m_id == target_merchant_id:
            merchant_found = True
            price_text = _format_egp_price(price_obj.get("money"))
            if price_text != "Not found":
                val = _parse_price_val(price_text)
                if val and val > 0:
                    basis = price_obj.get("savingBasis") or {}
                    basis_money = basis.get("money") if isinstance(basis, dict) else None
                    list_price_text = _format_egp_price(basis_money) if basis_money else None
                    if list_price_text == "Not found":
                        list_price_text = None
                    list_val = _parse_price_val(list_price_text) if list_price_text else None

                    seller_name = (merchant_info.get("name") or "").strip() or None
                    if seller_type == "NEW_AMAZON" and not seller_name:
                        seller_name = "Amazon.eg"

                    s_cond = _extract_offer_condition(listing)

                    matching_offers.append((val, price_text, list_price_text, list_val, seller_name, s_cond))

    if not merchant_found:
        return ("MISSING_MERCHANT", None, None, None, None, None, None)

    if not matching_offers:
        return ("OUT_OF_STOCK", None, None, None, None, None, None)

    matching_offers.sort(key=lambda x: x[0])
    best = matching_offers[0]
    return ("AVAILABLE", best[1], best[0], best[2], best[3], best[4], best[5])


def _pick_buy_box_listing(listings: list[dict]) -> dict | None:
    for listing in listings:
        if listing.get("isBuyBoxWinner"):
            return listing
    return listings[0] if listings else None


def _contains_arabic(text: str) -> bool:
    return any(
        "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" for ch in text
    )


def _extract_product_title(raw: dict[str, Any]) -> str:
    """Prefer an Arabic title from the Creators API payload when available."""
    title_obj = raw.get("itemInfo", {}).get("title", {})
    if not isinstance(title_obj, dict):
        return "Not found"

    display = (title_obj.get("displayValue") or "").strip()
    if display and _contains_arabic(display):
        return display

    candidates: list[str] = []
    for key in ("localizedDisplayValues", "displayValues", "values"):
        values = title_obj.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                text = (item.get("displayValue") or item.get("value") or "").strip()
            else:
                text = str(item).strip()
            if text:
                candidates.append(text)

    for value in title_obj.values():
        if isinstance(value, str):
            text = value.strip()
            if text and text != display:
                candidates.append(text)

    for candidate in candidates:
        if _contains_arabic(candidate):
            return candidate

    return display or "Not found"


def normalize_item(raw: dict[str, Any]) -> NormalizedItem | None:
    """Map Creators API item JSON to normalized bot structure."""
    asin = (raw.get("asin") or "").strip().upper()
    if not asin:
        return None

    title = _extract_product_title(raw)

    primary = raw.get("images", {}).get("primary", {})
    image_url = pick_best_primary_image_url(primary)
    if not image_url and isinstance(primary, dict):
        large = primary.get("large", {})
        if isinstance(large, dict):
            image_url = large.get("url") or None

    features: list[str] = []
    feat_obj = raw.get("itemInfo", {}).get("features", {})
    if isinstance(feat_obj, dict):
        values = feat_obj.get("displayValues") or []
        features = [str(v).strip() for v in values if str(v).strip()]

    detail_page_url = (raw.get("detailPageUrl") or "").strip()
    if not detail_page_url:
        detail_page_url = ""

    listings = raw.get("offersV2", {}).get("listings") or []
    listing = _pick_buy_box_listing(listings)
    price = "Not found"
    list_price: str | None = None
    prime_exclusive = False
    seller_name: str | None = None
    if listing:
        price_obj = listing.get("price") or {}
        price = _format_egp_price(price_obj.get("money"))
        basis = price_obj.get("savingBasis") or {}
        basis_money = basis.get("money") if isinstance(basis, dict) else None
        if basis_money:
            list_price = _format_egp_price(basis_money)
            if list_price == "Not found":
                list_price = None
        deal = listing.get("dealDetails") or {}
        if isinstance(deal, dict):
            access = (deal.get("accessType") or "").strip().upper()
            prime_exclusive = access == "PRIME_EXCLUSIVE"
        # Extract seller name from merchantInfo
        merchant_info = listing.get("merchantInfo") or {}
        if isinstance(merchant_info, dict):
            seller_name = (merchant_info.get("name") or "").strip() or None

    return NormalizedItem(
        asin=asin,
        title=title,
        price=price,
        image_url=image_url,
        features=features,
        detail_page_url=detail_page_url,
        list_price=list_price,
        prime_exclusive=prime_exclusive,
        seller_name=seller_name,
        raw_listings=listings,
    )


ASIN_PATTERN = re.compile(r"^[0-9]{9}[0-9X]|[A-Z][A-Z0-9]{9}$", re.IGNORECASE)


def is_valid_asin(asin: str | None) -> bool:
    if not asin:
        return False
    clean = asin.strip().upper()
    return len(clean) == 10 and bool(ASIN_PATTERN.match(clean))


@dataclass
class CreatorsClient:
    """
    Creators API client with token reuse and rate limiting.

    Always call get_items() — never single-ASIN-only wrappers internally.
    """

    credential_id: str = CREATORS_CREDENTIAL_ID
    credential_secret: str = CREATORS_CREDENTIAL_SECRET
    version: str = CREATORS_CREDENTIAL_VERSION
    marketplace: str = CREATORS_MARKETPLACE
    partner_tag: str = CREATORS_PARTNER_TAG
    _http: httpx.AsyncClient | None = field(default=None, repr=False)
    _token_manager: TokenManager | None = field(default=None, repr=False)
    _realtime_limiter: CreatorsRateLimiter = field(default_factory=lambda: CreatorsRateLimiter("REALTIME"))
    _monitoring_limiter: CreatorsRateLimiter = field(default_factory=lambda: CreatorsRateLimiter("MONITOR"))

    def __post_init__(self) -> None:
        if self._token_manager is None:
            self._token_manager = TokenManager(
                self.credential_id,
                self.credential_secret,
                self.version,
                http_client=self._http,
            )

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def record_monitoring_cooldown(self, duration: float) -> None:
        await self._monitoring_limiter.record_cooldown(duration)

    async def record_monitoring_success(self) -> None:
        await self._monitoring_limiter.record_success()

    def get_monitoring_cooldown_remaining(self) -> float:
        return self._monitoring_limiter.get_cooldown_remaining()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def get_items(
        self,
        asins: list[str],
        resources: list[str],
        *,
        db=None,
        profile: str = "draft",
        bypass_cache: bool = False,
    ) -> dict[str, NormalizedItem]:
        """
        Fetch 1–10 ASINs. Returns map asin -> NormalizedItem.
        Uses SQLite cache when db is provided unless bypass_cache is True.
        """
        if not asins:
            return {}

        normalized_asins = [a.strip().upper() for a in asins if is_valid_asin(a)]
        if not normalized_asins:
            return {}

        if len(normalized_asins) > 10:
            raise ValueError("Creators API accepts at most 10 ASINs per request")

        results: dict[str, NormalizedItem] = {}
        missing: list[str] = []

        if db is not None and not bypass_cache:
            for asin in normalized_asins:
                cached = db.get_creators_cache(asin, profile)
                if cached:
                    logger.debug("CREATORS CACHE HIT asin=%s profile=%s", asin, profile)
                    item = NormalizedItem.from_dict(cached)
                    if item.title != "Not found":
                        results[asin] = item
                        continue
                logger.debug("CREATORS CACHE MISS asin=%s profile=%s", asin, profile)
                missing.append(asin)
        else:
            if bypass_cache:
                logger.debug("CREATORS CACHE BYPASS asins=%s profile=%s", normalized_asins, profile)
            missing = list(normalized_asins)

        source_label = "MONITOR" if profile == "price_drop" else "REALTIME"
        limiter = self._monitoring_limiter if profile == "price_drop" else self._realtime_limiter

        for i in range(0, len(missing), 10):
            batch = missing[i : i + 10]
            batch_results = await self._fetch_items_batch(batch, resources, limiter=limiter, source=source_label)
            ttl = PROFILE_TTL_SECONDS.get(profile, 3600)
            cache_entries = []
            for asin, item in batch_results.items():
                results[asin] = item
                if db is not None:
                    item_dict = item.to_dict()
                    cache_entries.append((asin, profile, item_dict, ttl))
                    if profile in ("draft", "price_drop"):
                        other_prof = "price_drop" if profile == "draft" else "draft"
                        other_ttl = PROFILE_TTL_SECONDS.get(other_prof, 3600)
                        cache_entries.append((asin, other_prof, item_dict, other_ttl))
            if db is not None and cache_entries:
                db.set_creators_cache_bulk(cache_entries)

        return results

    async def _fetch_items_batch(
        self,
        asins: list[str],
        resources: list[str],
        limiter: CreatorsRateLimiter | None = None,
        source: str = "REALTIME",
    ) -> dict[str, NormalizedItem]:
        target_limiter = limiter or self._realtime_limiter
        await target_limiter.acquire(source=source)
        try:
            token = await self._token_manager.get_token()

            body = {
                "itemIds": asins,
                "itemIdType": "ASIN",
                "partnerTag": self.partner_tag,
                "marketplace": self.marketplace,
                "resources": resources,
            }
            languages_of_preference = _languages_of_preference(self.marketplace)
            if languages_of_preference:
                body["languagesOfPreference"] = languages_of_preference

            headers = {
                "Content-Type": "application/json",
                "Authorization": _auth_header(token, self.version),
                "x-marketplace": self.marketplace,
            }

            url = f"{CATALOG_BASE}/getItems"
            client = self._client()

            _log_creators_request(
                version=self.version,
                marketplace=self.marketplace,
                partner_tag=self.partner_tag,
                item_ids=asins,
                resources=resources,
                languages_of_preference=languages_of_preference,
            )
            _log_creators_headers(marketplace=self.marketplace)

            try:
                resp = await client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                raise CreatorsAPIError(f"HTTP error: {exc}") from exc

            response_text = resp.text or ""
            parsed = _log_creators_response(resp)

            if resp.status_code == 429:
                retry_after_hdr = resp.headers.get("retry-after") or resp.headers.get("Retry-After")
                retry_after_val: float | None = None
                if retry_after_hdr:
                    try:
                        retry_after_val = float(retry_after_hdr)
                    except ValueError:
                        pass
                raise CreatorsAPIError(
                    "Rate limited",
                    status_code=429,
                    response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
                    retry_after=retry_after_val,
                )
            if resp.status_code >= 500:
                raise CreatorsAPIError(
                    "Server error HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
                )
            if resp.status_code >= 400:
                raise CreatorsAPIError(
                    "Request failed HTTP {resp.status_code}",
                    status_code=resp.status_code,
                    response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
                )

            try:
                data = parsed if parsed is not None else resp.json()
            except json.JSONDecodeError as exc:
                raise CreatorsAPIError(
                    "Malformed JSON response",
                    response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
                ) from exc

            if data.get("errors"):
                logger.warning("CREATORS API partial errors: %s", data["errors"])

            items = (data.get("itemsResult") or {}).get("items") or []
            out: dict[str, NormalizedItem] = {}
            for raw in items:
                item = normalize_item(raw)
                if item:
                    out[item.asin] = item

            logger.info(
                "CREATORS API SUCCESS requested=%s returned=%s",
                len(asins),
                len(out),
            )
            return out
        finally:
            await target_limiter.release_request()


# Module-level singleton (initialized at bot startup when configured).
_client: CreatorsClient | None = None


def get_creators_client() -> CreatorsClient | None:
    return _client


async def init_creators_client() -> CreatorsClient | None:
    """Create shared client if credentials are configured."""
    global _client
    if not creators_api_configured():
        logger.info(
            "CREATORS CONFIG:\n"
            "enabled=False\n"
            "fallback_enabled=True"
        )
        return None
    _client = CreatorsClient()
    logger.info(
        "CREATORS CONFIG:\n"
        "enabled=True\n"
        "version=v%s\n"
        "marketplace=%s\n"
        "partner_tag=%s\n"
        "fallback_enabled=True",
        CREATORS_CREDENTIAL_VERSION,
        CREATORS_MARKETPLACE,
        _mask_partner_tag(CREATORS_PARTNER_TAG),
    )
    return _client


async def shutdown_creators_client() -> None:
    global _client
    if _client:
        await _client.close()
        _client = None
