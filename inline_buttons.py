"""Inline button management for product posts."""

import logging
import re
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# Marketing words to remove from product names
MARKETING_WORDS = {
    # Arabic
    "شاشة", "بوصة", "إنش", "مع", "بدون", "مقاس", "حجم", "لون", "ألوان",
    "عرض", "سعر", "خصم", "تخفيض", "عرض", "مميز", "خاص", "جديد", "أصلي",
    "أصلي", "ضمان", "سنة", "سنتين", "شهر", "أشهر", "يوم", "أيام",
    "متوفر", "غير متوفر", "مخزون", "كمية", "محدود", "محدود جداً",
    "شحن", "توصيل", "مجاني", "سريع", "توصيل مجاني", "شحن سريع",
    "أفضل", "الأفضل", "مميزات", "مواصفات", "تفاصيل", "وصف",
    "شراء", "اشتري", "احصل", "احصل عليه", "اطلب", "اطلب الآن",
    # English
    "inch", "inches", "with", "without", "size", "color", "colors",
    "display", "screen", "price", "discount", "sale", "offer", "special",
    "new", "original", "genuine", "warranty", "year", "years", "month", "months",
    "available", "unavailable", "stock", "quantity", "limited", "very limited",
    "shipping", "delivery", "free", "fast", "free shipping", "fast delivery",
    "best", "features", "specs", "details", "description",
    "buy", "purchase", "get", "order", "order now",
}

# Storage size patterns to remove (unless critical)
STORAGE_PATTERNS = [
    r'\d+\s*GB', r'\d+\s*TB', r'\d+\s*MB',
    r'\d+\s*جيجا', r'\d+\s*تيرا', r'\d+\s*ميجا',
]

# Screen size patterns to remove
SCREEN_PATTERNS = [
    r'\d+\.?\d*\s*"', r'\d+\.?\d*\s*inch', r'\d+\.?\d*\s*بوصة',
    r'\d+\.?\d*\s*إنش',
]


def short_product_name(title: str) -> str:
    """
    Extract short product name from full title.

    Goals:
    - Brand + Model whenever possible.
    - Never cut inside a word.
    - Remove marketing phrases.
    - Remove storage size unless important.
    - Remove screen sizes.
    - Remove unnecessary commas.
    - Max 32 characters.

    Examples:
    "اونر تابلت باد 10 شاشة 12.1 بوصة..." -> "Honor Pad 10"
    "Samsung Smart TV M80H 65-inch" -> "Samsung M80H"
    "Xiaomi Redmi Buds 6 Play Bluetooth Earbuds" -> "Redmi Buds 6"
    """
    if not title:
        return ""

    # Remove common punctuation and extra spaces
    cleaned = re.sub(r"[،,;:.\-–—/\\|(){}\[\]<>]", " ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Remove screen sizes
    for pattern in SCREEN_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Remove storage sizes (optional - keep if it's the main differentiator)
    # For now, remove them to keep names shorter
    for pattern in STORAGE_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    # Split into words
    words = cleaned.split()

    # Filter out marketing words
    filtered_words = []
    for word in words:
        # Check if word (case-insensitive) is a marketing word
        if word.lower() not in MARKETING_WORDS:
            filtered_words.append(word)

    if not filtered_words:
        return title[:32] if len(title) <= 32 else title[:29] + "..."

    # Try to extract brand + model (first 2-3 meaningful words)
    result_words = []
    for word in filtered_words[:4]:  # Take up to 4 words to find best fit
        if len(" ".join(result_words + [word])) <= 32:
            result_words.append(word)
        else:
            break

    result = " ".join(result_words)

    # If still too long, trim to last complete word
    if len(result) > 32:
        # Find last space before 32 chars
        last_space = result.rfind(" ", 0, 32)
        if last_space > 0:
            result = result[:last_space]
        else:
            result = result[:32]

    # If we trimmed and there's more content, add ellipsis
    if len(result) < len(" ".join(filtered_words)):
        if len(result) < 29:
            result += "..."

    return result


def build_inline_keyboard(
    products: list[dict[str, Any]],
    fixed_buttons: list[dict[str, Any]],
    product_buttons_enabled: bool = True,
    fixed_buttons_position: str = "BOTTOM",
    product_button_layout: str = "VERTICAL",
    product_button_template: str = "🛒 شراء {name}",
    max_product_buttons: int = 5,
) -> InlineKeyboardMarkup:
    """
    Build inline keyboard for product posts.

    Args:
        products: List of product dicts with 'title', 'url', and optionally 'asin' keys
        fixed_buttons: List of fixed button dicts with 'title' and 'url' keys
        product_buttons_enabled: Whether to show product buttons
        fixed_buttons_position: "TOP" or "BOTTOM" - where to place fixed buttons
        product_button_layout: "VERTICAL" or "TWO_COLUMNS" - layout for product buttons
        product_button_template: Template string with {name} placeholder for product name
        max_product_buttons: Maximum number of product buttons to generate (1-5)

    Returns:
        InlineKeyboardMarkup with product buttons and fixed buttons
    """
    keyboard = []

    # Build product buttons if enabled
    product_buttons = []
    duplicates_removed = 0
    if product_buttons_enabled and products:
        seen_asins = set()
        seen_urls = set()

        for product in products:
            # Stop if we've reached max
            if len(product_buttons) >= max_product_buttons:
                break

            title = product.get("title", "")
            url = product.get("url", "")
            asin = product.get("asin", "").upper()

            # Validate button
            if not title or not url:
                continue
            if not url.startswith(("http://", "https://")):
                continue

            # Deduplicate by ASIN or URL
            if asin and asin in seen_asins:
                duplicates_removed += 1
                continue
            if url in seen_urls:
                duplicates_removed += 1
                continue

            # Track seen
            if asin:
                seen_asins.add(asin)
            seen_urls.add(url)

            short_name = short_product_name(title)
            button_label = product_button_template.replace("{name}", short_name)
            product_buttons.append(InlineKeyboardButton(button_label, url=url))

        # Apply layout to product buttons
        if product_button_layout == "TWO_COLUMNS":
            # Arrange in two columns
            for i in range(0, len(product_buttons), 2):
                row = [product_buttons[i]]
                if i + 1 < len(product_buttons):
                    row.append(product_buttons[i + 1])
                keyboard.append(row)
        else:
            # Vertical layout (one button per row)
            for button in product_buttons:
                keyboard.append([button])

    # Build fixed buttons with validation and deduplication
    fixed_button_rows = []
    seen_fixed_urls = set()
    for button in fixed_buttons:
        title = button.get("title", "")
        url = button.get("url", "")
        enabled = button.get("enabled", 1)

        # Skip disabled buttons
        if not enabled:
            continue

        # Validate
        if not title or not url:
            continue
        if not url.startswith(("http://", "https://")):
            continue

        # Deduplicate by URL
        if url in seen_fixed_urls:
            continue
        seen_fixed_urls.add(url)

        fixed_button_rows.append([InlineKeyboardButton(title, url=url)])

    # Add separator if both exist
    if product_buttons and fixed_button_rows:
        keyboard.append([InlineKeyboardButton("─" * 20, callback_data="separator")])

    # Position fixed buttons
    if fixed_buttons_position == "TOP":
        # Fixed buttons first, then separator, then product buttons
        final_keyboard = fixed_button_rows
        if product_buttons:
            final_keyboard.append([InlineKeyboardButton("─" * 20, callback_data="separator")])
            final_keyboard.extend(keyboard)
        logger.info(
            "INLINE BUTTONS: products=%d generated=%d duplicates_removed=%d fixed=%d",
            len(products),
            len(product_buttons),
            duplicates_removed,
            len(fixed_button_rows),
        )
        return InlineKeyboardMarkup(final_keyboard)
    else:
        # Product buttons first, then separator, then fixed buttons (default)
        keyboard.extend(fixed_button_rows)
        logger.info(
            "INLINE BUTTONS: products=%d generated=%d duplicates_removed=%d fixed=%d",
            len(products),
            len(product_buttons),
            duplicates_removed,
            len(fixed_button_rows),
        )
        return InlineKeyboardMarkup(keyboard)
