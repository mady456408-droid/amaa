import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0") or "0")
DESTINATION_CHANNEL_ID = int(os.getenv("DESTINATION_CHANNEL_ID", "0") or "0")

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0") or "0")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")
TELEGRAM_SESSION_NAME = os.getenv("TELEGRAM_SESSION_NAME", "user")

DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db")
def _parse_admin_ids(raw: str) -> list[int]:
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            pass
    return ids


ADMIN_USER_IDS = _parse_admin_ids(os.getenv("ADMIN_USER_IDS", ""))

AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "www.amazon.eg")

# Amazon Creators API (primary product data source)
CREATORS_CREDENTIAL_ID = os.getenv("CREATORS_CREDENTIAL_ID", "")
CREATORS_CREDENTIAL_SECRET = os.getenv("CREATORS_CREDENTIAL_SECRET", "")
CREATORS_CREDENTIAL_VERSION = os.getenv("CREATORS_CREDENTIAL_VERSION", "2.2")
CREATORS_MARKETPLACE = os.getenv("CREATORS_MARKETPLACE", AMAZON_DOMAIN)
CREATORS_PARTNER_TAG = os.getenv("CREATORS_PARTNER_TAG", "")
CREATORS_API_TPS = float(os.getenv("CREATORS_API_TPS", "1"))
CREATORS_API_TPD = int(os.getenv("CREATORS_API_TPD", "8640"))
CREATORS_TOKEN_REFRESH_MARGIN_SEC = int(
    os.getenv("CREATORS_TOKEN_REFRESH_MARGIN_SEC", "300")
)
# When true (default), product images use Playwright screenshot + frame overlay.
FRAME_PRODUCT_IMAGES = os.getenv("FRAME_PRODUCT_IMAGES", "true").lower() in (
    "1",
    "true",
    "yes",
)

DEDUP_TTL_SECONDS = int(os.getenv("DEDUP_TTL_SECONDS", "3600"))
DEDUP_MAX_SIZE = int(os.getenv("DEDUP_MAX_SIZE", "2000"))

TELEGRAM_CONNECT_TIMEOUT = float(os.getenv("TELEGRAM_CONNECT_TIMEOUT", "30"))
TELEGRAM_READ_TIMEOUT = float(os.getenv("TELEGRAM_READ_TIMEOUT", "120"))
TELEGRAM_WRITE_TIMEOUT = float(os.getenv("TELEGRAM_WRITE_TIMEOUT", "120"))
TELEGRAM_POOL_TIMEOUT = float(os.getenv("TELEGRAM_POOL_TIMEOUT", "30"))

UPLOAD_MAX_BYTES = int(os.getenv("UPLOAD_MAX_BYTES", str(2 * 1024 * 1024)))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "95"))
PUBLISH_MAX_RETRIES = int(os.getenv("PUBLISH_MAX_RETRIES", "3"))

REDIRECT_TIMEOUT_SEC = float(os.getenv("REDIRECT_TIMEOUT_SEC", "10"))
PUBLISH_DELAY_SEC = float(os.getenv("PUBLISH_DELAY_SEC", "0.75"))
APPROVAL_TIMEOUT_MINUTES = int(os.getenv("APPROVAL_TIMEOUT_MINUTES", "30"))
LAST_PUBLISHED_LOOKBACK = 10

AI_CAPTION_ENABLED = os.getenv("AI_CAPTION_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
AI_PROVIDER = os.getenv("AI_PROVIDER", "gemini")
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.0-flash")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
AI_CAPTION_TIMEOUT = float(os.getenv("AI_CAPTION_TIMEOUT", "15"))

CHROMIUM_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

TITLE_SELECTORS = [
    "#productTitle",
    "h1 span",
]

PRICE_SELECTORS = [
    ".a-price .a-offscreen",
    "#corePrice_feature_div .a-offscreen",
    "#priceblock_ourprice",
    "#priceblock_dealprice",
    "#priceblock_saleprice",
]

# Amazon SiteStripe URL Shortener
AMAZON_SHORTENER_ENABLED = os.getenv("AMAZON_SHORTENER_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
)
AMAZON_SESSION_ID = os.getenv("AMAZON_SESSION_ID", "")
AMAZON_SESSION_TOKEN = os.getenv("AMAZON_SESSION_TOKEN", "")
AMAZON_UBID_ACBEG = os.getenv("AMAZON_UBID_ACBEG", "")
AMAZON_AT_ACBEG = os.getenv("AMAZON_AT_ACBEG", "")
AMAZON_SESS_AT_ACBEG = os.getenv("AMAZON_SESS_AT_ACBEG", "")
