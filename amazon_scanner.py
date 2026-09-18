"""
Amazon.eg Creators API SearchItems Scanner.

Standalone data collection & ASIN discovery tool.
Uses Amazon Creators API SearchItems + NewestArrivals to discover newly indexed ASINs.

Does NOT use Playwright, Selenium, browser scraping, or Telegram publishing.
"""

from __future__ import annotations

import asyncio
import argparse
import base64
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("amazon_scanner")

# Default search keywords for Trendyol discovery on Amazon.eg
DEFAULT_KEYWORDS = [
    "Trendyol",
    "Trendyol women",
    "Trendyol dress",
    "Trendyol shirt",
    "Trendyol shoes",
    "Trendyol bag",
    "Trendyol clothing",
]

PAGES_PER_KEYWORD = 10
ITEMS_PER_PAGE = 10
SORT_BY = "NewestArrivals"

# Confirmed-supported base resources for SearchItems
# Note: "parentASIN" is correct camelCase. Unsupported fields like merchantInfo/availability/type must NOT be included.
INITIAL_RESOURCES = [
    "itemInfo.title",
    "images.primary.small",
    "parentASIN",
    "offersV2.listings.price",
    "offersV2.listings.dealDetails",
    "offersV2.listings.condition",
    "offersV2.listings.isBuyBoxWinner",
    "browseNodeInfo.browseNodes",
]

SEARCH_ITEMS_URL = "https://creatorsapi.amazon/catalog/v1/searchItems"
DEFAULT_TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"


class ScannerError(Exception):
    """Base scanner exception."""


class RateLimitError(ScannerError):
    """HTTP 429 Rate Limit encountered."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class UnsupportedResourceError(ScannerError):
    """HTTP 400 Unsupported Resource error."""

    def __init__(self, message: str, invalid_resources: list[str]):
        super().__init__(message)
        self.invalid_resources = invalid_resources


class InvalidRequestError(ScannerError):
    """Non-retryable HTTP 400 parameter or request error."""


class FatalAuthError(ScannerError):
    """HTTP 401/403 Authentication error."""


class BudgetExhaustedError(ScannerError):
    """Daily TPD API quota exhausted."""


@dataclass
class Config:
    """Scanner Configuration."""

    credential_id: str = field(default_factory=lambda: os.getenv("CREATORS_CREDENTIAL_ID", ""))
    credential_secret: str = field(default_factory=lambda: os.getenv("CREATORS_CREDENTIAL_SECRET", ""))
    credential_version: str = field(default_factory=lambda: os.getenv("CREATORS_CREDENTIAL_VERSION", "3.0"))
    marketplace: str = field(default_factory=lambda: os.getenv("CREATORS_MARKETPLACE", "www.amazon.eg"))
    partner_tag: str = field(default_factory=lambda: os.getenv("CREATORS_PARTNER_TAG", ""))
    tps_limit: float = field(
        default_factory=lambda: float(os.getenv("CREATORS_API_TPS_LIMIT", os.getenv("CREATORS_API_TPS", "1.0")))
    )
    tpd_limit: int = field(
        default_factory=lambda: int(os.getenv("CREATORS_API_TPD_LIMIT", os.getenv("CREATORS_API_TPD", "8640")))
    )
    token_url: str = field(
        default_factory=lambda: os.getenv("CREATORS_TOKEN_URL", DEFAULT_TOKEN_URL)
    )
    db_path: str = field(default_factory=lambda: os.getenv("DATABASE_PATH", "bot.db"))
    raw_dir: str = field(default_factory=lambda: os.getenv("SCANNER_RAW_DIR", "scanner_raw"))
    keywords: list[str] = field(default_factory=lambda: list(DEFAULT_KEYWORDS))

    def is_configured(self) -> bool:
        return bool(self.credential_id and self.credential_secret and self.partner_tag)


class TokenManager:
    """OAuth 2.0 token cache for Creators API V3 / V2."""

    def __init__(self, config: Config, http_client: httpx.AsyncClient | None = None):
        self.config = config
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
            # Refresh 300 seconds before expiry
            margin = min(300, max(0, expires_in - 60))
            self._expires_at = now + expires_in - margin
            logger.info("CREATORS OAUTH TOKEN REFRESHED (expires_in=%ds)", expires_in)
            return self._token

    async def _fetch_token(self) -> tuple[str, int]:
        client = self._http or httpx.AsyncClient(timeout=30.0)
        endpoint = self.config.token_url
        scope = "creatorsapi::default" if self.config.credential_version.startswith("3.") else "creatorsapi/default"

        body = {
            "grant_type": "client_credentials",
            "client_id": self.config.credential_id,
            "client_secret": self.config.credential_secret,
            "scope": scope,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        try:
            resp = await client.post(
                endpoint,
                content=urlencode(body),
                headers=headers,
            )
        finally:
            if self._http is None:
                await client.aclose()

        if resp.status_code >= 400:
            raise FatalAuthError(f"OAuth token request failed HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise FatalAuthError("OAuth response missing access_token")
        expires_in = int(data.get("expires_in") or 3600)
        return token, expires_in


class RateLimiter:
    """Rate limiter enforcing TPS interval, daily TPD quota, and global 429 cooldowns."""

    def __init__(self, tps: float = 1.0, tpd: int = 8640):
        self.tps = tps
        self.tpd = tpd
        self._min_interval = 1.0 / tps if tps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0
        self._day_key = ""
        self._day_count = 0
        self._cooldown_until = 0.0

    async def acquire(self, db_count: int = 0) -> None:
        async with self._lock:
            day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if day_key != self._day_key:
                self._day_key = day_key
                self._day_count = 0

            current_used = max(self._day_count, db_count)
            if current_used >= self.tpd:
                raise BudgetExhaustedError(f"Daily TPD quota exhausted ({current_used}/{self.tpd})")

            now = time.monotonic()
            base_time = max(now, self._cooldown_until)
            if self._last_request_time < base_time:
                target_time = base_time
            else:
                target_time = self._last_request_time + self._min_interval

            wait = max(0.0, target_time - now)
            self._last_request_time = target_time
            self._day_count += 1

        if wait > 0:
            await asyncio.sleep(wait)

    async def record_cooldown(self, duration: float) -> None:
        async with self._lock:
            now = time.monotonic()
            target = now + max(0.0, duration)
            if target > self._cooldown_until:
                self._cooldown_until = target
                logger.warning("RATE LIMIT COOLDOWN APPLIED for %.2fs", duration)


class Database:
    """SQLite Database manager for product discovery & price tracking."""

    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path).resolve())
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=10000;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS products (
                    asin TEXT PRIMARY KEY,
                    parent_asin TEXT,
                    title TEXT,
                    price REAL,
                    currency TEXT,
                    seller TEXT,
                    availability TEXT,
                    offer_type TEXT,
                    deal_start TEXT,
                    deal_end TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    first_price REAL,
                    lowest_price REAL,
                    highest_price REAL
                );

                CREATE TABLE IF NOT EXISTS search_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_id INTEGER NOT NULL,
                    asin TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    item_page INTEGER NOT NULL,
                    result_position INTEGER NOT NULL,
                    seen_at TEXT NOT NULL,
                    price REAL
                );

                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    total_requests INTEGER DEFAULT 0,
                    successful_requests INTEGER DEFAULT 0,
                    failed_requests INTEGER DEFAULT 0,
                    rate_limited_requests INTEGER DEFAULT 0,
                    new_asins INTEGER DEFAULT 0,
                    price_changes INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS price_changes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    old_price REAL NOT NULL,
                    new_price REAL NOT NULL,
                    change_amount REAL NOT NULL,
                    change_percent REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_search_results_asin ON search_results(asin);
                CREATE INDEX IF NOT EXISTS idx_search_results_scan ON search_results(scan_id);
                CREATE INDEX IF NOT EXISTS idx_price_changes_asin ON price_changes(asin);
                """
            )
            conn.commit()

    def get_today_requests_count(self) -> int:
        day_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT SUM(total_requests) as cnt FROM scan_runs WHERE started_at LIKE ?",
                (f"{day_prefix}%",),
            ).fetchone()
            return int(row["cnt"] or 0) if row else 0

    def start_scan_run(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO scan_runs (started_at, status) VALUES (?, ?)",
                (now, "RUNNING"),
            )
            conn.commit()
            return cur.lastrowid

    def finish_scan_run(
        self,
        scan_id: int,
        status: str,
        total: int,
        successful: int,
        failed: int,
        rate_limited: int,
        new_asins: int,
        price_changes: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE scan_runs
                SET finished_at = ?, status = ?, total_requests = ?, successful_requests = ?,
                    failed_requests = ?, rate_limited_requests = ?, new_asins = ?, price_changes = ?
                WHERE id = ?
                """,
                (now, status, total, successful, failed, rate_limited, new_asins, price_changes, scan_id),
            )
            conn.commit()

    def get_product(self, asin: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM products WHERE asin = ?", (asin.upper(),)).fetchone()
            return dict(row) if row else None

    def upsert_product(
        self,
        asin: str,
        parent_asin: str | None,
        title: str,
        price: float | None,
        currency: str | None,
        seller: str | None,
        availability: str | None,
        offer_type: str | None,
        deal_start: str | None,
        deal_end: str | None,
        timestamp: str,
    ) -> tuple[bool, dict[str, Any] | None]:
        """
        Upsert product into SQLite.
        Returns tuple: (is_new_asin, price_change_dict_or_None)
        """
        asin = asin.upper()
        existing = self.get_product(asin)

        if existing is None:
            # NEW ASIN
            first_price = price
            lowest_price = price
            highest_price = price
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO products (
                        asin, parent_asin, title, price, currency, seller, availability,
                        offer_type, deal_start, deal_end, first_seen, last_seen,
                        first_price, lowest_price, highest_price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asin,
                        parent_asin,
                        title,
                        price,
                        currency,
                        seller,
                        availability,
                        offer_type,
                        deal_start,
                        deal_end,
                        timestamp,
                        timestamp,
                        first_price,
                        lowest_price,
                        highest_price,
                    ),
                )
                conn.commit()
            return True, None

        # Existing ASIN -> Update last_seen, prices
        old_price = existing.get("price")
        low_p = existing.get("lowest_price")
        high_p = existing.get("highest_price")

        new_low = min(low_p, price) if (low_p is not None and price is not None) else (price or low_p)
        new_high = max(high_p, price) if (high_p is not None and price is not None) else (price or high_p)

        price_change_event = None
        if old_price is not None and price is not None and old_price != price and old_price > 0:
            change_amount = round(price - old_price, 2)
            change_percent = round(((price - old_price) / old_price) * 100.0, 2)
            price_change_event = {
                "asin": asin,
                "detected_at": timestamp,
                "old_price": old_price,
                "new_price": price,
                "change_amount": change_amount,
                "change_percent": change_percent,
            }
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO price_changes (asin, detected_at, old_price, new_price, change_amount, change_percent)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (asin, timestamp, old_price, price, change_amount, change_percent),
                )
                conn.commit()

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE products
                SET parent_asin = COALESCE(?, parent_asin),
                    title = COALESCE(?, title),
                    price = COALESCE(?, price),
                    currency = COALESCE(?, currency),
                    seller = COALESCE(?, seller),
                    availability = COALESCE(?, availability),
                    offer_type = COALESCE(?, offer_type),
                    deal_start = COALESCE(?, deal_start),
                    deal_end = COALESCE(?, deal_end),
                    last_seen = ?,
                    lowest_price = ?,
                    highest_price = ?
                WHERE asin = ?
                """,
                (
                    parent_asin,
                    title,
                    price,
                    currency,
                    seller,
                    availability,
                    offer_type,
                    deal_start,
                    deal_end,
                    timestamp,
                    new_low,
                    new_high,
                    asin,
                ),
            )
            conn.commit()

        return False, price_change_event

    def record_search_result(
        self,
        scan_id: int,
        asin: str,
        keyword: str,
        item_page: int,
        position: int,
        timestamp: str,
        price: float | None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO search_results (scan_id, asin, keyword, item_page, result_position, seen_at, price)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (scan_id, asin.upper(), keyword, item_page, position, timestamp, price),
            )
            conn.commit()


def extract_invalid_resources_from_error(error_data: Any) -> list[str]:
    """Parse HTTP 400 error payload to extract rejected resource names."""
    invalid = set()
    text = json.dumps(error_data) if isinstance(error_data, (dict, list)) else str(error_data)

    # Match resource paths like offersV2.listings.merchantInfo, itemInfo.title, ParentASIN
    matches = re.findall(r"\b(?:[a-zA-Z0-9_]+\.[a-zA-Z0-9_\.]+)|ParentASIN\b", text)
    for m in matches:
        invalid.add(m)

    # Also extract single-quoted / double-quoted tokens that match resource patterns
    quoted = re.findall(r"['\"]([a-zA-Z0-9\._]+)['\"]", text)
    for q in quoted:
        if "." in q or q.lower() in ("parentasin",):
            invalid.add(q)

    return sorted(list(invalid))


class CreatorsAPIClient:
    """HTTP Client for Amazon Creators API SearchItems requests."""

    def __init__(self, config: Config, token_manager: TokenManager, rate_limiter: RateLimiter):
        self.config = config
        self.token_manager = token_manager
        self.rate_limiter = rate_limiter
        self.resources = list(INITIAL_RESOURCES)
        self._http: httpx.AsyncClient | None = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
            self._http = None

    async def search_items(
        self,
        keyword: str,
        page: int,
        scan_id: int,
        db: Database,
    ) -> dict[str, Any]:
        """
        Execute SearchItems request with automatic HTTP 400 resource pruning and HTTP 429 backoff.
        Saves raw JSON response to scanner_raw/.
        """
        max_attempts = 3
        attempt = 0

        while attempt < max_attempts:
            attempt += 1
            await self.rate_limiter.acquire(db_count=db.get_today_requests_count())
            token = await self.token_manager.get_token()

            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-marketplace": self.config.marketplace,
            }

            body = {
                "partnerTag": self.config.partner_tag,
                "marketplace": self.config.marketplace,
                "keywords": keyword,
                "sortBy": SORT_BY,
                "itemPage": page,
                "itemCount": ITEMS_PER_PAGE,
                "resources": self.resources,
            }

            client = self._client()
            try:
                resp = await client.post(SEARCH_ITEMS_URL, json=body, headers=headers)
            except httpx.HTTPError as exc:
                if attempt >= max_attempts:
                    raise ScannerError(f"HTTP connection error: {exc}") from exc
                await asyncio.sleep(2.0 * attempt)
                continue

            # 1. SUCCESS (HTTP 200)
            if resp.status_code == 200:
                raw_data = resp.json()
                self._save_raw_response(scan_id, keyword, page, raw_data)
                return raw_data

            # 2. RATE LIMITED (HTTP 429)
            if resp.status_code == 429:
                retry_hdr = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
                retry_after: float = 10.0
                if retry_hdr:
                    try:
                        retry_after = float(retry_hdr)
                    except ValueError:
                        pass
                await self.rate_limiter.record_cooldown(retry_after)
                raise RateLimitError(f"HTTP 429 Rate Limited (Retry-After: {retry_after}s)", retry_after=retry_after)

            # 3. CLIENT ERROR / INVALID RESOURCE (HTTP 400)
            if resp.status_code == 400:
                error_body = resp.text
                try:
                    err_json = resp.json()
                except json.JSONDecodeError:
                    err_json = error_body

                invalid = extract_invalid_resources_from_error(err_json)
                rejected = [r for r in invalid if r in self.resources]

                if rejected:
                    logger.warning("HTTP 400 INVALID RESOURCE DETECTED: %r. Pruning from scanner configuration.", rejected)
                    for r in rejected:
                        self.resources.remove(r)
                    logger.info("Updated working resource list (%d resources): %r", len(self.resources), self.resources)
                    # Retry immediately with pruned resources
                    continue
                else:
                    raise InvalidRequestError(f"HTTP 400 Invalid Request: {error_body[:300]}")

            # 4. AUTH FAILURE (HTTP 401 / 403)
            if resp.status_code in (401, 403):
                raise FatalAuthError(f"HTTP {resp.status_code} Auth Failure: {resp.text[:300]}")

            # 5. SERVER ERROR (HTTP 5xx)
            if resp.status_code >= 500:
                if attempt >= max_attempts:
                    raise ScannerError(f"Server error HTTP {resp.status_code}: {resp.text[:200]}")
                await asyncio.sleep(3.0 * attempt)
                continue

            raise ScannerError(f"Unexpected HTTP {resp.status_code}: {resp.text[:200]}")

        raise ScannerError("Max retries exceeded for SearchItems request")

    def _save_raw_response(self, scan_id: int, keyword: str, page: int, data: dict[str, Any]) -> None:
        """Save raw JSON payload to scanner_raw/YYYY-MM-DD/scan_<scan_id>_<keyword>_page_<page>.json."""
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        dir_path = Path(self.config.raw_dir) / date_str
        dir_path.mkdir(parents=True, exist_ok=True)

        clean_keyword = re.sub(r"[^\w\-_]", "_", keyword.strip())
        filename = f"scan_{scan_id:06d}_{clean_keyword}_page_{page:02d}.json"
        file_path = dir_path / filename

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)


def parse_search_item(raw_item: dict[str, Any]) -> dict[str, Any]:
    """Parse raw Creators API SearchItems JSON item into structured scanner dictionary."""
    asin = str(raw_item.get("asin") or "").strip().upper()
    parent_asin = raw_item.get("parentASIN") or raw_item.get("parentAsin")
    if parent_asin:
        parent_asin = str(parent_asin).strip().upper()

    title_obj = raw_item.get("itemInfo", {}).get("title", {})
    title = title_obj.get("displayValue") or "Not found"

    listings = raw_item.get("offersV2", {}).get("listings") or []
    listing = None
    for l in listings:
        if l.get("isBuyBoxWinner"):
            listing = l
            break
    if not listing and listings:
        listing = listings[0]

    price_val: float | None = None
    currency: str | None = None
    availability: str | None = None
    offer_type: str | None = None
    deal_start: str | None = None
    deal_end: str | None = None

    if listing:
        price_obj = listing.get("price") or {}
        money = price_obj.get("money") or {}
        if money.get("amount") is not None:
            try:
                price_val = float(money["amount"])
            except (ValueError, TypeError):
                pass
        currency = money.get("currency") or "EGP"

        avail_obj = listing.get("availability") or {}
        if isinstance(avail_obj, dict):
            availability = avail_obj.get("type") or avail_obj.get("message")
        elif isinstance(avail_obj, str):
            availability = avail_obj

        deal_obj = listing.get("dealDetails") or {}
        if isinstance(deal_obj, dict):
            offer_type = deal_obj.get("accessType") or deal_obj.get("badge")
            deal_start = deal_obj.get("startTime")
            deal_end = deal_obj.get("endTime")

    # Merchant info is NOT exposed by SearchItems API
    seller = "N/A (not provided by SearchItems)"

    return {
        "asin": asin,
        "parent_asin": parent_asin,
        "title": title,
        "price": price_val,
        "currency": currency,
        "seller": seller,
        "availability": availability,
        "offer_type": offer_type,
        "deal_start": deal_start,
        "deal_end": deal_end,
    }


def print_new_asin_box(item: dict[str, Any], keyword: str, page: int, position: int, timestamp: str) -> None:
    price_str = f"{item['price']:.2f} {item['currency'] or 'EGP'}" if item["price"] is not None else "N/A"
    print("=" * 50)
    print("NEW ASIN")
    print("=" * 50)
    print(f"ASIN: {item['asin']}")
    print(f"Title: {item['title']}")
    print(f"Price: {price_str}")
    print(f"Seller: {item['seller']}")
    print(f"First Seen: {timestamp}")
    print(f"Keyword: {keyword}")
    print(f"Page: {page}")
    print(f"Position: {position}")
    print(f"Detail URL: https://www.amazon.eg/dp/{item['asin']}")
    print("=" * 50)


def print_price_change_box(change: dict[str, Any], title: str, keyword: str) -> None:
    old_p = change["old_price"]
    new_p = change["new_price"]
    amt = change["change_amount"]
    pct = change["change_percent"]
    sign_amt = f"+{amt:.2f}" if amt > 0 else f"{amt:.2f}"
    sign_pct = f"+{pct:.2f}%" if pct > 0 else f"{pct:.2f}%"

    print("=" * 50)
    print("PRICE CHANGE")
    print("=" * 50)
    print(f"ASIN: {change['asin']}")
    print(f"Title: {title}")
    print(f"Old Price: {old_p:.2f} EGP")
    print(f"New Price: {new_p:.2f} EGP")
    print(f"Change: {sign_amt} EGP")
    print(f"Change %: {sign_pct}")
    print(f"Detected: {change['detected_at']}")
    print(f"Keyword: {keyword}")
    print("=" * 50)


class Scanner:
    """Scanner Engine orchestrating single and continuous scans."""

    def __init__(self, config: Config, db: Database, client: CreatorsAPIClient):
        self.config = config
        self.db = db
        self.client = client

    async def run_single_scan(self) -> str:
        """
        Execute full scan across all configured keywords and pages.
        Returns scan status: COMPLETED, PARTIAL, or FAILED.
        """
        total_expected_requests = len(self.config.keywords) * PAGES_PER_KEYWORD
        used_today = self.db.get_today_requests_count()
        remaining_budget = max(0, self.config.tpd_limit - used_today)

        print(f"\nSTARTING SCAN RUN:")
        print(f"  Keywords: {len(self.config.keywords)}")
        print(f"  Pages per Keyword: {PAGES_PER_KEYWORD}")
        print(f"  Total Requests Planned: {total_expected_requests}")
        print(f"  Configured TPS: {self.config.tps_limit}")
        print(f"  Configured TPD: {self.config.tpd_limit}")
        print(f"  Used Today: {used_today}")
        print(f"  Remaining Budget: {remaining_budget}\n")

        if remaining_budget < total_expected_requests:
            logger.warning(
                "TPD budget insufficient for full scan (planned=%d, remaining=%d). Scan will run partially.",
                total_expected_requests,
                remaining_budget,
            )

        scan_id = self.db.start_scan_run()
        total_requests = 0
        successful_requests = 0
        failed_requests = 0
        rate_limited_requests = 0
        new_asins_count = 0
        price_changes_count = 0
        status = "COMPLETED"

        for kw in self.config.keywords:
            for page in range(1, PAGES_PER_KEYWORD + 1):
                total_requests += 1
                now_iso = datetime.now(timezone.utc).isoformat()

                try:
                    raw_data = await self.client.search_items(kw, page, scan_id, self.db)
                    successful_requests += 1

                    items = (raw_data.get("searchResult") or raw_data.get("itemsResult") or {}).get("items") or []
                    for pos, raw_item in enumerate(items, start=1):
                        item = parse_search_item(raw_item)
                        if not item["asin"]:
                            continue

                        is_new, price_change = self.db.upsert_product(
                            asin=item["asin"],
                            parent_asin=item["parent_asin"],
                            title=item["title"],
                            price=item["price"],
                            currency=item["currency"],
                            seller=item["seller"],
                            availability=item["availability"],
                            offer_type=item["offer_type"],
                            deal_start=item["deal_start"],
                            deal_end=item["deal_end"],
                            timestamp=now_iso,
                        )

                        self.db.record_search_result(
                            scan_id=scan_id,
                            asin=item["asin"],
                            keyword=kw,
                            item_page=page,
                            position=pos,
                            timestamp=now_iso,
                            price=item["price"],
                        )

                        if is_new:
                            new_asins_count += 1
                            print_new_asin_box(item, kw, page, pos, now_iso)
                        elif price_change:
                            price_changes_count += 1
                            print_price_change_box(price_change, item["title"], kw)

                except RateLimitError as exc:
                    rate_limited_requests += 1
                    status = "PARTIAL"
                    logger.warning("Rate limit encountered on kw=%s page=%d: %s", kw, page, exc)
                except BudgetExhaustedError as exc:
                    status = "PARTIAL"
                    logger.error("Scan stopped due to TPD budget exhaustion: %s", exc)
                    break
                except FatalAuthError as exc:
                    status = "FAILED"
                    logger.error("Scan failed due to fatal authentication error: %s", exc)
                    break
                except ScannerError as exc:
                    failed_requests += 1
                    logger.error("Scan request failed on kw=%s page=%d: %s", kw, page, exc)

            if status in ("FAILED", "PARTIAL") and "budget" in locals().get("exc", "").lower():
                break

        if failed_requests > 0 and status == "COMPLETED":
            status = "PARTIAL"

        self.db.finish_scan_run(
            scan_id=scan_id,
            status=status,
            total=total_requests,
            successful=successful_requests,
            failed=failed_requests,
            rate_limited=rate_limited_requests,
            new_asins=new_asins_count,
            price_changes=price_changes_count,
        )

        print(f"\nSCAN END")
        print(f"  status={status}")
        print(f"  requests={successful_requests}/{total_requests}")
        print(f"  new_asins={new_asins_count}")
        print(f"  price_changes={price_changes_count}")
        print(f"  final_resources={self.client.resources}\n")

        return status


class Scheduler:
    """Execution mode controller (--once vs --loop)."""

    def __init__(self, config: Config):
        self.config = config

    async def run(self, once: bool = False, interval: int = 300) -> None:
        db = Database(self.config.db_path)
        token_mgr = TokenManager(self.config)
        rate_limiter = RateLimiter(tps=self.config.tps_limit, tpd=self.config.tpd_limit)
        client = CreatorsAPIClient(self.config, token_mgr, rate_limiter)

        print("=" * 60)
        print("AMAZON.EG CREATORS API SEARCHITEMS SCANNER")
        print("=" * 60)
        print(f"Configured TPS: {self.config.tps_limit}")
        print(f"Configured TPD: {self.config.tpd_limit}")
        print(f"Estimated Requests per Scan: {len(self.config.keywords) * PAGES_PER_KEYWORD}")
        est_scans = math.floor(self.config.tpd_limit / (len(self.config.keywords) * PAGES_PER_KEYWORD)) if (len(self.config.keywords) * PAGES_PER_KEYWORD) > 0 else 0
        print(f"Max Supported Scans/Day: {est_scans}")
        print("=" * 60)

        if not self.config.is_configured():
            logger.error(
                "Missing required Creators API environment variables! "
                "Ensure CREATORS_CREDENTIAL_ID, CREATORS_CREDENTIAL_SECRET, and CREATORS_PARTNER_TAG are set."
            )

        scanner = Scanner(self.config, db, client)

        try:
            if once:
                await scanner.run_single_scan()
                return

            while True:
                start_time = time.monotonic()
                try:
                    await scanner.run_single_scan()
                except Exception as exc:
                    logger.error("Unhandled error during scan iteration: %s", exc, exc_info=True)

                elapsed = time.monotonic() - start_time
                sleep_time = max(0.0, float(interval) - elapsed)
                if sleep_time > 0:
                    logger.info("Sleeping %.1fs until next scan loop...", sleep_time)
                    await asyncio.sleep(sleep_time)
                else:
                    logger.warning("Scan took %.1fs (longer than interval %ds). Starting next scan immediately.", elapsed, interval)
        finally:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Amazon.eg Creators API SearchItems Scanner")
    parser.add_argument("--once", action="store_true", help="Run a single scan and exit")
    parser.add_argument("--loop", action="store_true", help="Run continuously in loop mode")
    parser.add_argument("--interval", type=int, default=300, help="Scan interval in seconds for loop mode (default: 300)")

    args = parser.parse_args()
    config = Config()

    if not args.once and not args.loop:
        args.once = True  # Default to once if unassigned

    try:
        asyncio.run(Scheduler(config).run(once=args.once, interval=args.interval))
    except KeyboardInterrupt:
        print("\nScanner stopped by user.")


if __name__ == "__main__":
    main()
