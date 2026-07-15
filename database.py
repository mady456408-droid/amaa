import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SETTING_DESTINATION = "destination_channel_id"
SETTING_PAUSED = "bot_paused"
SETTING_AI_CAPTION_MODE = "ai_caption_mode"
SETTING_AI_CUSTOM_PROMPT = "ai_custom_prompt"
SETTING_AFFILIATE_TAG_ENABLED = "affiliate_tag_enabled"
SETTING_AFFILIATE_TAG_VALUE = "affiliate_tag_value"
SETTING_COUPON_DETECTION_ENABLED = "coupon_detection_enabled"
SETTING_PRODUCT_BUTTONS_ENABLED = "product_buttons_enabled"
SETTING_FIXED_BUTTONS_POSITION = "fixed_buttons_position"
SETTING_PRODUCT_BUTTON_LAYOUT = "product_button_layout"
SETTING_PRODUCT_BUTTON_TEMPLATE = "product_button_template"
SETTING_MAX_PRODUCT_BUTTONS = "max_product_buttons"
SETTING_MIN_PRICE_DROP = "min_price_drop"
SETTING_GEMINI_ENABLED = "gemini_enabled"
SETTING_GEMINI_MODEL = "gemini_model"
SETTING_GEMINI_SYSTEM_PROMPT = "gemini_system_prompt"
SETTING_GEMINI_TEMPERATURE = "gemini_temperature"
SETTING_GEMINI_MAX_TOKENS = "gemini_max_tokens"


class Database:
    def __init__(self, db_path: str):
        self.db_path = str(Path(db_path))
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id INTEGER NOT NULL UNIQUE,
                    channel_name TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS published_products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_channel_id INTEGER NOT NULL,
                    destination_message_id INTEGER,
                    published_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_approvals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT NOT NULL DEFAULT '',
                    clean_url TEXT NOT NULL,
                    source_channel_id INTEGER NOT NULL,
                    caption TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_published_asin_at
                    ON published_products (asin, published_at DESC);
                CREATE TABLE IF NOT EXISTS draft_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asin TEXT NOT NULL,
                    title TEXT NOT NULL,
                    price TEXT NOT NULL DEFAULT '',
                    clean_url TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    created_by INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_pending_status_created
                    ON pending_approvals (status, created_at);
                CREATE INDEX IF NOT EXISTS idx_draft_status_created
                    ON draft_posts (status, created_at);

                CREATE TABLE IF NOT EXISTS destinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    chat_id INTEGER NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_destinations_enabled_order
                    ON destinations (enabled, sort_order);
                """
            )
            conn.commit()
        self._migrate_schema()
        logger.info("Database ready: %s", self.db_path)

    def _migrate_schema(self) -> None:
        with self._connect() as conn:
            pending_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(pending_approvals)").fetchall()
            }
            if "price" not in pending_cols:
                conn.execute(
                    "ALTER TABLE pending_approvals ADD COLUMN price TEXT NOT NULL DEFAULT ''"
                )
            if "coupon" not in pending_cols:
                conn.execute("ALTER TABLE pending_approvals ADD COLUMN coupon TEXT")
            if "list_price" not in pending_cols:
                conn.execute("ALTER TABLE pending_approvals ADD COLUMN list_price TEXT")

            draft_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(draft_posts)").fetchall()
            }
            if "coupon" not in draft_cols:
                conn.execute("ALTER TABLE draft_posts ADD COLUMN coupon TEXT")
            if "list_price" not in draft_cols:
                conn.execute("ALTER TABLE draft_posts ADD COLUMN list_price TEXT")
            if "short_title" not in draft_cols:
                conn.execute("ALTER TABLE draft_posts ADD COLUMN short_title TEXT")

            published_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(published_products)").fetchall()
            }
            for col, col_type in (
                ("published_price", "TEXT"),
                ("published_price_value", "REAL"),
                ("published_list_price", "TEXT"),
                ("published_list_price_value", "REAL"),
                ("published_currency", "TEXT"),
                ("last_checked_at", "TEXT"),
                ("destination_id", "INTEGER"),
            ):
                if col not in published_cols:
                    conn.execute(f"ALTER TABLE published_products ADD COLUMN {col} {col_type}")

            # Add optimized index for destination-aware lookups
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_published_destination_asin_at "
                "ON published_products (destination_id, asin, published_at DESC)"
            )

            # Add additional columns for price monitoring
            for col, col_type in (
                ("last_price_check", "REAL"),
                ("previous_channel_id", "INTEGER"),
                ("previous_message_id", "INTEGER"),
                ("previous_published_price", "TEXT"),
                ("previous_published_at", "TEXT"),
            ):
                if col not in published_cols:
                    conn.execute(
                        f"ALTER TABLE published_products ADD COLUMN {col} {col_type}"
                    )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creators_cache (
                    asin TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (asin, profile)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_drop_tracked_asins (
                    asin TEXT PRIMARY KEY,
                    last_price TEXT,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS shortened_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    affiliate_url TEXT NOT NULL UNIQUE,
                    short_url TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creators_image_url_cache (
                    asin TEXT PRIMARY KEY,
                    image_url TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS creators_title_cache (
                    asin TEXT PRIMARY KEY,
                    english_title TEXT NOT NULL,
                    arabic_title TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fixed_buttons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gemini_rewrite_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caption_hash TEXT NOT NULL UNIQUE,
                    original_caption TEXT NOT NULL,
                    rewritten_caption TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def seed_from_env(self, source_channel_id: int, destination_channel_id: int) -> None:
        if source_channel_id and source_channel_id != 0:
            if not self.get_source_by_channel_id(source_channel_id):
                self.add_source(
                    source_channel_id,
                    "Env source",
                    active=True,
                )
                logger.info("Seeded source channel %s from env", source_channel_id)

        if destination_channel_id and destination_channel_id != 0:
            if not self.get_setting(SETTING_DESTINATION):
                self.set_setting(SETTING_DESTINATION, str(destination_channel_id))
                logger.info("Seeded destination %s from env", destination_channel_id)

    def add_source(
        self,
        channel_id: int,
        channel_name: str,
        *,
        active: bool = True,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO sources (channel_id, channel_name, active, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (channel_id, channel_name, 1 if active else 0, now),
                )
                conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def remove_source(self, channel_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM sources WHERE channel_id = ?",
                (channel_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_source_active(self, channel_id: int, active: bool) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE sources SET active = ? WHERE channel_id = ?",
                (1 if active else 0, channel_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_source_by_channel_id(self, channel_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_sources(self, active_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM sources"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def get_active_channel_ids(self) -> set[int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT channel_id FROM sources WHERE active = 1"
            ).fetchall()
        return {int(r["channel_id"]) for r in rows}

    def get_setting(self, key: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()

    def get_destination_channel_id(self) -> int | None:
        raw = self.get_setting(SETTING_DESTINATION)
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def set_destination_channel_id(self, channel_id: int) -> None:
        self.set_setting(SETTING_DESTINATION, str(channel_id))

    def is_paused(self) -> bool:
        return self.get_setting(SETTING_PAUSED) == "1"

    def set_paused(self, paused: bool) -> None:
        self.set_setting(SETTING_PAUSED, "1" if paused else "0")

    def get_ai_caption_mode(self) -> str:
        return self.get_setting(SETTING_AI_CAPTION_MODE) or "off"

    def set_ai_caption_mode(self, mode: str) -> None:
        self.set_setting(SETTING_AI_CAPTION_MODE, mode)

    def get_ai_custom_prompt(self) -> str | None:
        return self.get_setting(SETTING_AI_CUSTOM_PROMPT)

    def set_ai_custom_prompt(self, prompt: str) -> None:
        self.set_setting(SETTING_AI_CUSTOM_PROMPT, prompt)

    def get_affiliate_tag_enabled(self) -> bool:
        return (self.get_setting(SETTING_AFFILIATE_TAG_ENABLED) or "0") == "1"

    def set_affiliate_tag_enabled(self, enabled: bool) -> None:
        self.set_setting(SETTING_AFFILIATE_TAG_ENABLED, "1" if enabled else "0")

    def get_affiliate_tag_value(self) -> str:
        return (self.get_setting(SETTING_AFFILIATE_TAG_VALUE) or "").strip()

    def set_affiliate_tag_value(self, value: str) -> None:
        self.set_setting(SETTING_AFFILIATE_TAG_VALUE, (value or "").strip())

    def get_coupon_detection_enabled(self) -> bool:
        raw = self.get_setting(SETTING_COUPON_DETECTION_ENABLED)
        if raw is None:
            return True
        return raw == "1"

    def set_coupon_detection_enabled(self, enabled: bool) -> None:
        self.set_setting(SETTING_COUPON_DETECTION_ENABLED, "1" if enabled else "0")

    def get_product_buttons_enabled(self) -> bool:
        raw = self.get_setting(SETTING_PRODUCT_BUTTONS_ENABLED)
        if raw is None:
            return True  # Default ON
        return raw == "1"

    def set_product_buttons_enabled(self, enabled: bool) -> None:
        self.set_setting(SETTING_PRODUCT_BUTTONS_ENABLED, "1" if enabled else "0")

    def get_fixed_buttons_position(self) -> str:
        return self.get_setting(SETTING_FIXED_BUTTONS_POSITION) or "BOTTOM"

    def set_fixed_buttons_position(self, position: str) -> None:
        self.set_setting(SETTING_FIXED_BUTTONS_POSITION, position)

    def get_product_button_layout(self) -> str:
        return self.get_setting(SETTING_PRODUCT_BUTTON_LAYOUT) or "VERTICAL"

    def set_product_button_layout(self, layout: str) -> None:
        self.set_setting(SETTING_PRODUCT_BUTTON_LAYOUT, layout)

    def get_product_button_template(self) -> str:
        return self.get_setting(SETTING_PRODUCT_BUTTON_TEMPLATE) or "🛒 شراء {name}"

    def set_product_button_template(self, template: str) -> None:
        self.set_setting(SETTING_PRODUCT_BUTTON_TEMPLATE, template)

    def get_max_product_buttons(self) -> int:
        raw = self.get_setting(SETTING_MAX_PRODUCT_BUTTONS)
        if raw is None:
            return 5  # Default 5
        try:
            val = int(raw)
            return max(1, min(5, val))  # Clamp between 1 and 5
        except ValueError:
            return 5

    def set_max_product_buttons(self, count: int) -> None:
        clamped = max(1, min(5, count))
        self.set_setting(SETTING_MAX_PRODUCT_BUTTONS, str(clamped))

    def get_min_price_drop(self) -> int:
        raw = self.get_setting(SETTING_MIN_PRICE_DROP)
        if raw is None:
            return 1  # Default
        try:
            val = int(raw)
            return max(1, min(10000, val))  # Clamp between 1 and 10000
        except ValueError:
            return 1

    def set_min_price_drop(self, value: int) -> None:
        clamped = max(1, min(10000, value))
        self.set_setting(SETTING_MIN_PRICE_DROP, str(clamped))

    # --- Gemini AI Rewrite Settings ---

    def get_gemini_enabled(self) -> bool:
        return self.get_setting(SETTING_GEMINI_ENABLED) == "1"

    def set_gemini_enabled(self, enabled: bool) -> None:
        self.set_setting(SETTING_GEMINI_ENABLED, "1" if enabled else "0")

    def get_gemini_model(self) -> str:
        return self.get_setting(SETTING_GEMINI_MODEL) or "gemini-2.5-flash"

    def set_gemini_model(self, model: str) -> None:
        self.set_setting(SETTING_GEMINI_MODEL, model)

    def get_gemini_system_prompt(self) -> str:
        return self.get_setting(SETTING_GEMINI_SYSTEM_PROMPT) or ""

    def set_gemini_system_prompt(self, prompt: str) -> None:
        self.set_setting(SETTING_GEMINI_SYSTEM_PROMPT, prompt)

    def get_gemini_temperature(self) -> float:
        raw = self.get_setting(SETTING_GEMINI_TEMPERATURE)
        if raw is None:
            return 0.7  # Default
        try:
            val = float(raw)
            return max(0.0, min(2.0, val))  # Clamp between 0.0 and 2.0
        except ValueError:
            return 0.7

    def set_gemini_temperature(self, temperature: float) -> None:
        clamped = max(0.0, min(2.0, temperature))
        self.set_setting(SETTING_GEMINI_TEMPERATURE, str(clamped))

    def get_gemini_max_tokens(self) -> int:
        raw = self.get_setting(SETTING_GEMINI_MAX_TOKENS)
        if raw is None:
            return 1024  # Default
        try:
            val = int(raw)
            return max(1, min(8192, val))  # Clamp between 1 and 8192
        except ValueError:
            return 1024

    def set_gemini_max_tokens(self, max_tokens: int) -> None:
        clamped = max(1, min(8192, max_tokens))
        self.set_setting(SETTING_GEMINI_MAX_TOKENS, str(clamped))

    # --- Gemini Rewrite Cache ---

    def _hash_caption(self, caption: str) -> str:
        """Generate SHA256 hash of caption for cache key."""
        return hashlib.sha256(caption.encode("utf-8")).hexdigest()

    def get_gemini_rewrite_cache(self, caption: str) -> str | None:
        """Get cached rewritten caption if exists."""
        caption_hash = self._hash_caption(caption)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rewritten_caption FROM gemini_rewrite_cache WHERE caption_hash = ?",
                (caption_hash,),
            ).fetchone()
        return row["rewritten_caption"] if row else None

    def set_gemini_rewrite_cache(self, original_caption: str, rewritten_caption: str) -> None:
        """Cache a rewritten caption."""
        caption_hash = self._hash_caption(original_caption)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gemini_rewrite_cache (caption_hash, original_caption, rewritten_caption, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(caption_hash) DO UPDATE SET
                    original_caption = excluded.original_caption,
                    rewritten_caption = excluded.rewritten_caption,
                    created_at = excluded.created_at
                """,
                (caption_hash, original_caption, rewritten_caption, now),
            )
            conn.commit()

    def clear_gemini_rewrite_cache(self) -> int:
        """Clear all gemini rewrite cache entries. Returns number of rows deleted."""
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM gemini_rewrite_cache")
            conn.commit()
            return cur.rowcount

    def get_gemini_cache_size(self) -> int:
        """Get current cache size (number of entries)."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) as count FROM gemini_rewrite_cache").fetchone()
        return row["count"] if row else 0

    def get_last_published_asins(self, limit: int = 10) -> set[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT asin FROM published_products
                ORDER BY published_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return {str(r["asin"]).upper() for r in rows}

    def is_asin_in_last_published(self, asin: str, limit: int = 10) -> bool:
        return asin.upper() in self.get_last_published_asins(limit)

    def is_asin_in_last_published_for_destination(
        self, asin: str, destination_id: int, limit: int = 10
    ) -> bool:
        """Check if ASIN was recently published to a specific destination."""
        asin = asin.upper()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=limit)
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM published_products
                WHERE asin = ? AND destination_id = ? AND published_at > ?
                LIMIT 1
                """,
                (asin, destination_id, cutoff.isoformat()),
            ).fetchone()
        return row is not None

    def add_published_product(
        self,
        asin: str,
        title: str,
        source_channel_id: int,
        destination_message_id: int | None,
        *,
        destination_id: int | None = None,
        published_price: str | None = None,
        published_price_value: float | None = None,
        published_list_price: str | None = None,
        published_list_price_value: float | None = None,
        published_currency: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO published_products
                    (asin, title, source_channel_id, destination_message_id, published_at,
                     destination_id, published_price, published_price_value, published_list_price,
                     published_list_price_value, published_currency)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asin.upper(),
                    title,
                    source_channel_id,
                    destination_message_id,
                    now,
                    destination_id,
                    published_price,
                    published_price_value,
                    published_list_price,
                    published_list_price_value,
                    published_currency,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_published_product(self, published_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM published_products WHERE id = ?",
                (published_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_unique_published_products(self) -> list[dict[str, Any]]:
        """Most recent published row per unique ASIN."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT p.*
                FROM published_products p
                INNER JOIN (
                    SELECT asin, MAX(published_at) AS max_at
                    FROM published_products
                    GROUP BY asin
                ) latest ON p.asin = latest.asin AND p.published_at = latest.max_at
                ORDER BY p.published_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def update_published_product_price_check(
        self,
        published_id: int,
        last_price_check: float | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE published_products
                SET last_checked_at = ?, last_price_check = ?
                WHERE id = ?
                """,
                (now, last_price_check, published_id),
            )
            conn.commit()

    def update_published_product_after_republish(
        self,
        published_id: int,
        *,
        title: str,
        source_channel_id: int,
        destination_message_id: int,
        destination_id: int | None = None,
        published_price: str | None,
        published_price_value: float | None,
        published_list_price: str | None,
        published_list_price_value: float | None,
        published_currency: str | None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            # First, get current values to preserve as previous_*
            current = conn.execute(
                "SELECT * FROM published_products WHERE id = ?",
                (published_id,),
            ).fetchone()

            if current:
                conn.execute(
                    """
                    UPDATE published_products
                    SET previous_channel_id = source_channel_id,
                        previous_message_id = destination_message_id,
                        previous_published_price = published_price,
                        previous_published_at = published_at,
                        title = ?,
                        source_channel_id = ?,
                        destination_message_id = ?,
                        destination_id = ?,
                        published_at = ?,
                        published_price = ?,
                        published_price_value = ?,
                        published_list_price = ?,
                        published_list_price_value = ?,
                        published_currency = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        source_channel_id,
                        destination_message_id,
                        destination_id,
                        now,
                        published_price,
                        published_price_value,
                        published_list_price,
                        published_list_price_value,
                        published_currency,
                        published_id,
                    ),
                )
            else:
                # Fallback if record not found (shouldn't happen)
                conn.execute(
                    """
                    UPDATE published_products
                    SET title = ?,
                        source_channel_id = ?,
                        destination_message_id = ?,
                        destination_id = ?,
                        published_at = ?,
                        published_price = ?,
                        published_price_value = ?,
                        published_list_price = ?,
                        published_list_price_value = ?,
                        published_currency = ?
                    WHERE id = ?
                    """,
                    (
                        title,
                        source_channel_id,
                        destination_message_id,
                        destination_id,
                        now,
                        published_price,
                        published_price_value,
                        published_list_price,
                        published_list_price_value,
                        published_currency,
                        published_id,
                    ),
                )
            conn.commit()

    def create_pending_approval(
        self,
        asin: str,
        title: str,
        price: str,
        clean_url: str,
        source_channel_id: int,
        caption: str,
        image_path: str,
        coupon: str | None = None,
        list_price: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO pending_approvals
                    (asin, title, price, clean_url, source_channel_id, caption,
                     image_path, status, created_at, coupon, list_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    asin.upper(),
                    title,
                    price,
                    clean_url,
                    source_channel_id,
                    caption,
                    image_path,
                    now,
                    coupon,
                    list_price,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_pending_approval(self, pending_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_approvals WHERE id = ?",
                (pending_id,),
            ).fetchone()
        return dict(row) if row else None

    def set_pending_status(self, pending_id: int, status: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE pending_approvals SET status = ? WHERE id = ? AND status = 'pending'",
                (status, pending_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_expired_pending_approvals(self, older_than_minutes: int) -> list[dict[str, Any]]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)
        ).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_approvals
                WHERE status = 'pending' AND created_at <= ?
                """,
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]

    def create_draft_post(
        self,
        asin: str,
        title: str,
        price: str,
        clean_url: str,
        caption: str,
        image_path: str,
        created_by: int,
        coupon: str | None = None,
        list_price: str | None = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO draft_posts
                    (asin, title, price, clean_url, caption, image_path, status,
                     created_at, created_by, coupon, list_price)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    asin.upper(),
                    title,
                    price,
                    clean_url,
                    caption,
                    image_path,
                    now,
                    created_by,
                    coupon,
                    list_price,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_draft_post(self, draft_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM draft_posts WHERE id = ?",
                (draft_id,),
            ).fetchone()
        return dict(row) if row else None

    def update_draft_caption(self, draft_id: int, caption: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE draft_posts SET caption = ?
                WHERE id = ? AND status = 'draft'
                """,
                (caption, draft_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def set_draft_status(self, draft_id: int, status: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE draft_posts SET status = ? WHERE id = ? AND status = 'draft'",
                (status, draft_id),
            )
            conn.commit()
            return cur.rowcount > 0

    # --- Creators API cache ---

    def get_creators_cache(self, asin: str, profile: str) -> dict[str, Any] | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT payload_json, expires_at FROM creators_cache
                WHERE asin = ? AND profile = ? AND expires_at > ?
                """,
                (asin.upper(), profile, now),
            ).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None

    def set_creators_cache(
        self,
        asin: str,
        profile: str,
        payload: dict[str, Any],
        *,
        ttl_seconds: int,
    ) -> None:
        now = datetime.now(timezone.utc)
        expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creators_cache (asin, profile, payload_json, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(asin, profile) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    asin.upper(),
                    profile,
                    json.dumps(payload, ensure_ascii=False),
                    expires,
                    now.isoformat(),
                ),
            )
            conn.commit()

    # --- Creators API product image URL cache (best resolved CDN URL per ASIN) ---

    def get_creators_image_url(self, asin: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT image_url FROM creators_image_url_cache WHERE asin = ?",
                (asin.upper(),),
            ).fetchone()
        if not row:
            return None
        url = (row["image_url"] or "").strip()
        return url or None

    def set_creators_image_url(self, asin: str, image_url: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creators_image_url_cache (asin, image_url, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    image_url = excluded.image_url,
                    updated_at = excluded.updated_at
                """,
                (asin.upper(), image_url, now),
            )
            conn.commit()

    # --- Creators API Arabic frame title cache ---

    def get_creators_title_cache(self, asin: str) -> dict[str, str] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT english_title, arabic_title
                FROM creators_title_cache
                WHERE asin = ?
                """,
                (asin.upper(),),
            ).fetchone()
        if not row:
            return None
        english = (row["english_title"] or "").strip()
        arabic = (row["arabic_title"] or "").strip()
        if not english or not arabic:
            return None
        return {"english_title": english, "arabic_title": arabic}

    def set_creators_title_cache(
        self,
        asin: str,
        english_title: str,
        arabic_title: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO creators_title_cache (asin, english_title, arabic_title, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    english_title = excluded.english_title,
                    arabic_title = excluded.arabic_title,
                    updated_at = excluded.updated_at
                """,
                (asin.upper(), english_title, arabic_title, now),
            )
            conn.commit()

    # --- Price drop tracking (infrastructure for future alerts) ---

    def upsert_tracked_asin(self, asin: str, *, last_price: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO price_drop_tracked_asins (asin, last_price, last_checked_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    last_price = COALESCE(excluded.last_price, price_drop_tracked_asins.last_price),
                    last_checked_at = excluded.last_checked_at
                """,
                (asin.upper(), last_price, now, now),
            )
            conn.commit()

    def list_tracked_asins(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM price_drop_tracked_asins
                ORDER BY COALESCE(last_checked_at, '') ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_tracked_asin_price(self, asin: str, last_price: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE price_drop_tracked_asins
                SET last_price = ?, last_checked_at = ?
                WHERE asin = ?
                """,
                (last_price, now, asin.upper()),
            )
            conn.commit()

    # --- Amazon SiteStripe URL Shortener cache ---

    def get_shortened_link(self, affiliate_url: str) -> str | None:
        """Get cached short URL for affiliate URL."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT short_url FROM shortened_links WHERE affiliate_url = ?",
                (affiliate_url,),
            ).fetchone()
        return row["short_url"] if row else None

    def save_shortened_link(self, affiliate_url: str, short_url: str) -> None:
        """Save or update shortened link for affiliate URL."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO shortened_links (affiliate_url, short_url, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(affiliate_url) DO UPDATE SET
                    short_url = excluded.short_url,
                    created_at = excluded.created_at
                """,
                (affiliate_url, short_url, now),
            )
            conn.commit()

    # --- Fixed Buttons management ---

    def create_fixed_button(
        self, title: str, url: str, enabled: bool = True, sort_order: int = 0
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO fixed_buttons (title, url, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, url, 1 if enabled else 0, sort_order, now, now),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_fixed_button(self, button_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM fixed_buttons WHERE id = ?",
                (button_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_fixed_buttons(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM fixed_buttons"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY sort_order ASC, id ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [dict(r) for r in rows]

    def update_fixed_button(
        self,
        button_id: int,
        title: str | None = None,
        url: str | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> bool:
        updates = []
        params = []
        now = datetime.now(timezone.utc).isoformat()

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if url is not None:
            updates.append("url = ?")
            params.append(url)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if sort_order is not None:
            updates.append("sort_order = ?")
            params.append(sort_order)

        if not updates:
            return False

        updates.append("updated_at = ?")
        params.append(now)
        params.append(button_id)

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE fixed_buttons SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
            return cur.rowcount > 0

    def delete_fixed_button(self, button_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM fixed_buttons WHERE id = ?",
                (button_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # Destination CRUD methods

    def add_destination(
        self,
        title: str,
        chat_id: int,
        enabled: bool = True,
        sort_order: int = 0,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO destinations (title, chat_id, enabled, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (title, chat_id, 1 if enabled else 0, sort_order, now, now),
            )
            conn.commit()
        return cur.lastrowid

    def get_destination(self, destination_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM destinations WHERE id = ?",
                (destination_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_destinations(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM destinations"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY sort_order ASC, id ASC"
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        result = [dict(r) for r in rows]
        logger.info("list_destinations(enabled_only=%s) returned %s destinations", enabled_only, len(result))
        for dest in result:
            logger.info("  - id=%s title=%s chat_id=%s enabled=%s sort_order=%s",
                       dest.get("id"), dest.get("title"), dest.get("chat_id"),
                       dest.get("enabled"), dest.get("sort_order"))
        return result

    def update_destination(
        self,
        destination_id: int,
        title: str | None = None,
        chat_id: int | None = None,
        enabled: bool | None = None,
        sort_order: int | None = None,
    ) -> bool:
        updates = []
        params = []
        now = datetime.now(timezone.utc).isoformat()

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if chat_id is not None:
            updates.append("chat_id = ?")
            params.append(chat_id)
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if sort_order is not None:
            updates.append("sort_order = ?")
            params.append(sort_order)

        if not updates:
            return False

        updates.append("updated_at = ?")
        params.append(now)
        params.append(destination_id)

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE destinations SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
        return cur.rowcount > 0

    def delete_destination(self, destination_id: int) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM destinations WHERE id = ?",
                (destination_id,),
            )
            conn.commit()
        return cur.rowcount > 0

    def move_destination_up(self, destination_id: int) -> bool:
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM destinations WHERE id = ?",
                (destination_id,),
            ).fetchone()
            if not current:
                return False

            current_order = current["sort_order"]
            prev = conn.execute(
                """
                SELECT * FROM destinations
                WHERE sort_order < ? AND enabled = 1
                ORDER BY sort_order DESC
                LIMIT 1
                """,
                (current_order,),
            ).fetchone()

            if not prev:
                return False

            conn.execute(
                "UPDATE destinations SET sort_order = ? WHERE id = ?",
                (prev["sort_order"], destination_id),
            )
            conn.execute(
                "UPDATE destinations SET sort_order = ? WHERE id = ?",
                (current_order, prev["id"]),
            )
            conn.commit()
        return True

    def move_destination_down(self, destination_id: int) -> bool:
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM destinations WHERE id = ?",
                (destination_id,),
            ).fetchone()
            if not current:
                return False

            current_order = current["sort_order"]
            next_dest = conn.execute(
                """
                SELECT * FROM destinations
                WHERE sort_order > ? AND enabled = 1
                ORDER BY sort_order ASC
                LIMIT 1
                """,
                (current_order,),
            ).fetchone()

            if not next_dest:
                return False

            conn.execute(
                "UPDATE destinations SET sort_order = ? WHERE id = ?",
                (next_dest["sort_order"], destination_id),
            )
            conn.execute(
                "UPDATE destinations SET sort_order = ? WHERE id = ?",
                (current_order, next_dest["id"]),
            )
            conn.commit()
        return True

    def get_enabled_destinations(self) -> list[dict[str, Any]]:
        return self.list_destinations(enabled_only=True)
