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
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from amazon_image_url import pick_best_primary_image_url
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
]

PRICE_DROP_PROFILE: list[str] = [
    "offersV2.listings.price",
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
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


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
            logger.info("CREATORS RESPONSE JSON:\n%r", parsed)
        else:
            logger.info("CREATORS RESPONSE JSON: parsed_ok keys=%r", list(parsed.keys()))
    else:
        logger.info("CREATORS RESPONSE JSON: unavailable")

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
    """Global asyncio-safe limiter: TPS + daily quota."""

    def __init__(self, *, tps: float = CREATORS_API_TPS, tpd: int = CREATORS_API_TPD):
        self._min_interval = 1.0 / tps if tps > 0 else 0.0
        self._tpd = tpd
        self._lock = asyncio.Lock()
        self._last_request = 0.0
        self._day_key = ""
        self._day_count = 0

    async def acquire(self) -> None:
        async with self._lock:
            day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if day_key != self._day_key:
                self._day_key = day_key
                self._day_count = 0

            if self._day_count >= self._tpd:
                raise CreatorsAPIError(
                    "Daily Creators API quota exceeded",
                    status_code=429,
                )

            now = time.monotonic()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)

            self._last_request = time.monotonic()
            self._day_count += 1


def _format_egp_price(money: dict[str, Any] | None) -> str:
    """Format API money object as Egyptian Pounds display string."""
    if not money:
        return "Not found"
    display = (money.get("displayAmount") or "").strip()
    if display:
        if "جنيه" in display or "EGP" in display.upper():
            return display
        return f"{display} جنيه"
    amount = money.get("amount")
    if amount is not None:
        try:
            val = float(amount)
            if abs(val - round(val)) < 0.01:
                return f"{int(round(val))} جنيه"
            return f"{val:.2f} جنيه"
        except (TypeError, ValueError):
            pass
    return "Not found"


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
    )


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
    _limiter: CreatorsRateLimiter = field(default_factory=CreatorsRateLimiter)

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
    ) -> dict[str, NormalizedItem]:
        """
        Fetch 1–10 ASINs. Returns map asin -> NormalizedItem.
        Uses SQLite cache when db is provided.
        """
        if not asins:
            return {}
        if len(asins) > 10:
            raise ValueError("Creators API accepts at most 10 ASINs per request")

        normalized_asins = [a.strip().upper() for a in asins]
        results: dict[str, NormalizedItem] = {}
        missing: list[str] = []

        if db is not None:
            for asin in normalized_asins:
                cached = db.get_creators_cache(asin, profile)
                if cached:
                    logger.info("CREATORS CACHE HIT asin=%s profile=%s", asin, profile)
                    item = NormalizedItem.from_dict(cached)
                    if item.title != "Not found":
                        results[asin] = item
                        continue
                logger.info("CREATORS CACHE MISS asin=%s profile=%s", asin, profile)
                missing.append(asin)
        else:
            missing = list(normalized_asins)

        for i in range(0, len(missing), 10):
            batch = missing[i : i + 10]
            batch_results = await self._fetch_items_batch(batch, resources)
            ttl = PROFILE_TTL_SECONDS.get(profile, 3600)
            for asin, item in batch_results.items():
                results[asin] = item
                if db is not None:
                    db.set_creators_cache(
                        asin,
                        profile,
                        item.to_dict(),
                        ttl_seconds=ttl,
                    )

        return results

    async def _fetch_items_batch(
        self,
        asins: list[str],
        resources: list[str],
    ) -> dict[str, NormalizedItem]:
        await self._limiter.acquire()
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

        # Diagnostic logs — no secrets, no raw Authorization value.
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
            raise CreatorsAPIError(
                "Rate limited",
                status_code=429,
                response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
            )
        if resp.status_code >= 500:
            raise CreatorsAPIError(
                f"Server error HTTP {resp.status_code}",
                status_code=resp.status_code,
                response_body=response_text[:_RESPONSE_BODY_LOG_LIMIT],
            )
        if resp.status_code >= 400:
            raise CreatorsAPIError(
                f"Request failed HTTP {resp.status_code}",
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
