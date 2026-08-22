from __future__ import annotations

import os
from typing import NamedTuple

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from coupon_price import parse_price_number
from PIL import features

_HAS_RAQM = features.check("raqm")

# Global typography scale - adjust to scale all typography uniformly
_FONT_SCALE = 1.15

_FRAME_PADDING = 40
_LEFT_PANEL_RATIO_MIN = 0.36
_LEFT_PANEL_RATIO_MAX = 0.40
_OLD_PRICE_CARD_GAP = 24
_TITLE_OLD_PRICE_GAP = 28
_PRICE_SELLER_GAP = 14
_SELLER_LINE_GAP = 6
_IMG_PANEL_PAD = 16
_IMG_FILL_RATIO = 0.966
_TALL_ASPECT_THRESHOLD = 0.88
_INFO_PAD = 28
_INFO_TOP_OFFSET = 6
_TITLE_MAX_LINES = 3
_TITLE_AFTER_GAP = 20
_TITLE_FONT_MAX = int(72 * _FONT_SCALE)
_TITLE_FONT_MIN = int(40 * _FONT_SCALE)
_TITLE_LINE_GAP = int(14 * _FONT_SCALE)
_OLD_PRICE_FONT = int(68 * _FONT_SCALE)
_OLD_PRICE_STRIKE_WIDTH = int(6 * _FONT_SCALE)
_PRICE_LABEL_FONT = int(32 * _FONT_SCALE)
_PRICE_CURRENCY_FONT = int(42 * _FONT_SCALE)
_PRICE_NUM_MAX = int(110 * _FONT_SCALE)
_PRICE_NUM_MIN = int(68 * _FONT_SCALE)
_PRICE_CARD_PAD_X = 48
_PRICE_CARD_PAD_Y = 32
_PRICE_CARD_RADIUS = 26
_PRICE_CARD_INNER_RESERVE = 90
_PRICE_CARD_WIDTH_BOOST = 1.12
_PRICE_NUM_CURRENCY_GAP = 11
_DISCOUNT_BADGE_FONT = int(48 * _FONT_SCALE)
_DISCOUNT_BADGE_PAD_X = 30
_DISCOUNT_BADGE_PAD_Y = 16
_DISCOUNT_BADGE_RADIUS = 20
_AMAZON_YELLOW = (255, 216, 20, 255)
_AMAZON_YELLOW_BORDER = (235, 195, 10, 255)
_GRAY_TEXT = (86, 89, 89, 255)
_LABEL_GRAY = (86, 89, 89, 255)
_BLACK_TEXT = (15, 17, 17, 255)
_DISCOUNT_RED = (204, 12, 57, 255)
_PRIME_BLUE_LIGHT = (0, 168, 225, 255)
_PRIME_BADGE_FONT = int(32 * _FONT_SCALE)
_PRIME_BADGE_PAD_X = 22
_PRIME_BADGE_PAD_Y = 10
_PRIME_BADGE_RADIUS = 16
_WHITE_THRESHOLD = 248
_TRANSPARENT_ALPHA = 12
_CORNER_BADGE_MARGIN = 32
_PRICE_LABEL = "السعر الآن"
_OLD_PRICE_LABEL = "بدلاً من"
_CURRENCY_LABEL = "جنيه"
_COMPOSITE_MIN_PRODUCTS = 2
_COMPOSITE_MAX_PRODUCTS = 6
_COMPOSITE_OUTER_PAD = 24
_COMPOSITE_GRID_GAP = 20
_COMPOSITE_CARD_PAD = 12
_COMPOSITE_IMG_TEXT_GAP = 10
_COMPOSITE_TITLE_PRICE_GAP = 8
_COMPOSITE_TITLE_FONT_MAX = int(30 * _FONT_SCALE)
_COMPOSITE_TITLE_FONT_MIN = int(17 * _FONT_SCALE)
_COMPOSITE_PORTRAIT_HEIGHT_RATIO = 1.18
_COMPOSITE_MIN_IMAGE_RATIO = 0.42


class CreatorsProductCard(NamedTuple):
    image_path: str
    title: str | None = None
    price: str | None = None
    list_price: str | None = None
    prime_exclusive: bool = False
    seller_name: str | None = None
    seller_condition: str | None = None


class CompositeCardSlot(NamedTuple):
    x: int
    y: int
    width: int
    height: int
    portrait: bool = False


class FrameGeometry(NamedTuple):
    frame_width: int
    frame_height: int
    slot_x: int
    slot_y: int
    slot_width: int
    slot_height: int


def get_frame_geometry(frame: Image.Image) -> FrameGeometry:
    """Derive content slot placement from the frame image size."""
    frame_w, frame_h = frame.size
    padding = _FRAME_PADDING
    return FrameGeometry(
        frame_width=frame_w,
        frame_height=frame_h,
        slot_x=padding,
        slot_y=padding,
        slot_width=frame_w - padding * 2,
        slot_height=frame_h - padding * 2,
    )


def apply_frame(screenshot_path, output_path="framed_output.png"):
    frame_path = "frame.png"

    frame = Image.open(frame_path).convert("RGBA")
    geo = get_frame_geometry(frame)
    screenshot = Image.open(screenshot_path).convert("RGBA")

    screenshot = screenshot.resize(
        (geo.slot_width, geo.slot_height), Image.Resampling.LANCZOS
    )

    canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    canvas.paste(screenshot, (geo.slot_x, geo.slot_y))

    final = Image.alpha_composite(canvas, frame)

    final.save(output_path)

    return output_path


def _trim_product_borders(image: Image.Image) -> Image.Image:
    """Remove transparent and uniform white margins around the product."""
    rgba = image.convert("RGBA")
    bbox = _content_bbox(rgba)
    if bbox:
        return rgba.crop(bbox)
    return rgba


def _content_bbox(img: Image.Image) -> tuple[int, int, int, int] | None:
    rgba = img.convert("RGBA")
    width, height = rgba.size
    pixels = rgba.load()

    min_x, min_y = width, height
    max_x, max_y = 0, 0
    found = False

    for y in range(height):
        for x in range(width):
            if not _is_border_pixel(pixels[x, y]):
                found = True
                if x < min_x:
                    min_x = x
                if y < min_y:
                    min_y = y
                if x > max_x:
                    max_x = x
                if y > max_y:
                    max_y = y

    if found:
        return min_x, min_y, max_x + 1, max_y + 1

    alpha_bbox = rgba.split()[3].getbbox()
    return alpha_bbox


def _is_border_pixel(pixel: tuple[int, int, int, int]) -> bool:
    r, g, b, a = pixel
    if a < _TRANSPARENT_ALPHA:
        return True
    return (
        r >= _WHITE_THRESHOLD
        and g >= _WHITE_THRESHOLD
        and b >= _WHITE_THRESHOLD
    )


def _neutralize_transparent_rgb(img: Image.Image) -> Image.Image:
    """Clear dark RGB under transparent pixels so LANCZOS resize does not fringe."""
    rgba = img.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if a < _TRANSPARENT_ALPHA:
                pixels[x, y] = (255, 255, 255, 0)
            elif a < 255 and (r < 32 and g < 32 and b < 32):
                pixels[x, y] = (255, 255, 255, a)
    return rgba


def _left_panel_width_bounds(slot_width: int) -> tuple[int, int]:
    return (
        int(slot_width * _LEFT_PANEL_RATIO_MIN),
        int(slot_width * _LEFT_PANEL_RATIO_MAX),
    )


def _compute_left_panel_width(
    draw: ImageDraw.ImageDraw,
    slot_width: int,
    *,
    title: str | None,
    price: str | None,
    list_price: str | None,
    seller_name: str | None = None,
) -> int:
    """Size the info panel to content, clamped to ~30–32% of slot width."""
    min_w, max_w = _left_panel_width_bounds(slot_width)
    trial_inner = max_w - 2 * _INFO_PAD
    content_w = _measure_info_content_width(
        draw,
        title=title,
        price=price,
        list_price=list_price,
        seller_name=seller_name,
        panel_width=trial_inner,
    )
    needed = content_w + 2 * _INFO_PAD
    left_w = max(min_w, min(max_w, needed))
    final_inner = left_w - 2 * _INFO_PAD
    refined = _measure_info_content_width(
        draw,
        title=title,
        price=price,
        list_price=list_price,
        seller_name=seller_name,
        panel_width=final_inner,
    ) + 2 * _INFO_PAD
    return max(min_w, min(max_w, refined))


def _contains_arabic(text: str) -> bool:
    return any(
        "\u0600" <= ch <= "\u06FF" or "\u0750" <= ch <= "\u077F" for ch in text
    )


def shape_text(text: str) -> str:
    if not text:
        return text
    if not _contains_arabic(text):
        return text
    
    # Linux / Railway
    if _HAS_RAQM:
        return arabic_reshaper.reshape(text)

    # Windows
    return get_display(arabic_reshaper.reshape(text))


def _draw_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
) -> None:
    draw.text(xy, shape_text(text), font=font, fill=fill)


def _valid_price(text: str | None) -> bool:
    return bool(text) and text.strip() != "Not found"


def _compute_product_scale(trimmed_w: int, trimmed_h: int, area_w: int, area_h: int) -> float:
    if trimmed_w <= 0 or trimmed_h <= 0:
        return 1.0
    return min(
        area_w * _IMG_FILL_RATIO / trimmed_w,
        area_h * _IMG_FILL_RATIO / trimmed_h,
    )


def _compute_product_position(
    area_w: int,
    area_h: int,
    scaled_w: int,
    scaled_h: int,
    aspect: float,
) -> tuple[int, int]:
    empty_x = area_w - scaled_w
    empty_y = area_h - scaled_h
    rel_x = empty_x // 2
    if aspect < _TALL_ASPECT_THRESHOLD:
        rel_y = int(empty_y * 0.28)
    else:
        rel_y = empty_y // 2
    return rel_x, rel_y


def apply_frame_creators_product(
    image_path: str,
    output_path: str,
    *,
    asin: str | None = None,
    title: str | None = None,
    price: str | None = None,
    list_price: str | None = None,
    prime_exclusive: bool = False,
    seller_name: str | None = None,
    seller_condition: str | None = None,
    seller_type: str = "NEW_AMAZON",
    merchant_id: str | None = None,
    **kwargs,
) -> str:
    """
    Premium Creators API product card: info panel left, product image right.
    Frame artwork and dimensions match apply_frame(); only inner content differs.
    """
    frame_path = "frame.png"
    frame = Image.open(frame_path).convert("RGBA")
    geo = get_frame_geometry(frame)

    canvas = Image.new("RGBA", (geo.slot_width, geo.slot_height), (255, 255, 255, 255))

    probe = ImageDraw.Draw(canvas)
    left_w = _compute_left_panel_width(
        probe,
        geo.slot_width,
        title=title,
        price=price,
        list_price=list_price,
    )
    right_w = geo.slot_width - left_w
    inner_w = right_w - 2 * _IMG_PANEL_PAD
    inner_h = geo.slot_height - 2 * _IMG_PANEL_PAD

    image = _neutralize_transparent_rgb(_trim_product_borders(Image.open(image_path)))
    trimmed_w, trimmed_h = image.size
    aspect = trimmed_w / trimmed_h if trimmed_h else 1.0
    scale = _compute_product_scale(trimmed_w, trimmed_h, inner_w, inner_h)
    scaled_w = max(1, int(trimmed_w * scale))
    scaled_h = max(1, int(trimmed_h * scale))
    image_scaled = image.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)

    rel_x, rel_y = _compute_product_position(inner_w, inner_h, scaled_w, scaled_h, aspect)
    product_area = _composite_on_white((inner_w, inner_h), image_scaled, (rel_x, rel_y))
    canvas.paste(product_area, (left_w + _IMG_PANEL_PAD, _IMG_PANEL_PAD))

    panel_width = left_w - 2 * _INFO_PAD
    layout_scale = _panel_content_scale(
        probe,
        title=title,
        price=price,
        list_price=list_price,
        panel_width=panel_width,
        slot_height=geo.slot_height,
    )

    _draw_info_panel(
        canvas,
        left_w=left_w,
        slot_height=geo.slot_height,
        title=title,
        price=price,
        list_price=list_price,
        seller_name=seller_name,
        seller_condition=seller_condition,
        layout_scale=layout_scale,
    )
    _draw_corner_badges(
        canvas,
        price=price,
        list_price=list_price,
        prime_exclusive=prime_exclusive,
        layout_scale=layout_scale,
    )

    final_canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    final_canvas.paste(canvas, (geo.slot_x, geo.slot_y))
    final = Image.alpha_composite(final_canvas, frame)
    final.save(output_path)
    return output_path


def _contains_latin_or_ascii_product_chars(text: str) -> bool:
    """True when text includes Latin letters or ASCII digits/symbols."""
    for ch in text:
        if ch in " \t\n":
            continue
        if "A" <= ch <= "Z" or "a" <= ch <= "z":
            return True
        if ch.isascii() and (ch.isdigit() or not ch.isalpha()):
            return True
    return False


def _is_mixed_script_title(text: str) -> bool:
    """Arabic plus Latin letters or ASCII product codes/symbols."""
    return _contains_arabic(text) and _contains_latin_or_ascii_product_chars(text)


def _try_font_path(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    if not os.path.isfile(path):
        return None
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


def _first_usable_font(
    paths: list[str],
    size: int,
) -> ImageFont.FreeTypeFont | None:
    seen: set[str] = set()
    for path in paths:
        normalized = os.path.normcase(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        font = _try_font_path(path, size)
        if font is not None:
            return font
    return None


_MIXED_SCRIPT_TITLE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
]

_ARABIC_TITLE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Bold.ttf",
    "C:/Windows/Fonts/NotoNaskhArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
    "C:/Windows/Fonts/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoKufiArabic-Bold.ttf",
    "C:/Windows/Fonts/NotoKufiArabic-Bold.ttf",
]

_LATIN_TITLE_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Black.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Black.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-ExtraBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-ExtraBold.ttf",
    "C:/Windows/Fonts/seguibl.ttf",
    "C:/Windows/Fonts/ariblk.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

_ARABIC_TITLE_FONT_FALLBACKS = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
    "C:/Windows/Fonts/NotoSansArabic-Bold.ttf",
]

_MIXED_SCRIPT_UI_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
]

_ARABIC_UI_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoNaskhArabic-Regular.ttf",
    "C:/Windows/Fonts/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
    "C:/Windows/Fonts/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoKufiArabic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoKufiArabic-Regular.ttf",
    "C:/Windows/Fonts/NotoKufiArabic-Regular.ttf",
]

_ARABIC_UI_FONT_FALLBACKS_REGULAR = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
]

_LATIN_UI_FONT_CANDIDATES_REGULAR = [
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _script_font_candidates(text: str, *, bold: bool) -> tuple[list[str], list[str]]:
    """Return (primary, fallback) font paths for text using script-based policy."""
    if _is_mixed_script_title(text):
        if bold:
            return _MIXED_SCRIPT_TITLE_FONT_CANDIDATES, []
        return _MIXED_SCRIPT_UI_FONT_CANDIDATES_REGULAR, []

    if _contains_arabic(text):
        if bold:
            return _ARABIC_TITLE_FONT_CANDIDATES, _ARABIC_TITLE_FONT_FALLBACKS
        return _ARABIC_UI_FONT_CANDIDATES_REGULAR, _ARABIC_UI_FONT_FALLBACKS_REGULAR

    if bold:
        return _LATIN_TITLE_FONT_CANDIDATES, []
    return _LATIN_UI_FONT_CANDIDATES_REGULAR, []


def _load_ui_font(
    size: int,
    text: str,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    primary, fallbacks = _script_font_candidates(text, bold=bold)
    font = _first_usable_font(primary, size)
    if font is not None:
        return font
    if fallbacks:
        font = _first_usable_font(fallbacks, size)
        if font is not None:
            return font
    return _load_font(size, bold=bold)


def _load_title_font(
    size: int,
    *,
    title: str | None = None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _load_ui_font(size, title or "", bold=True)


def _load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    regular_candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    bold_candidates = [
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    candidates = bold_candidates if bold else regular_candidates
    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    for path in regular_candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _composite_on_white(
    canvas_size: tuple[int, int],
    overlay: Image.Image,
    position: tuple[int, int],
) -> Image.Image:
    """Composite RGBA overlay onto pure white using alpha (anti-aliased edges)."""
    white_bg = Image.new("RGBA", canvas_size, (255, 255, 255, 255))
    layer = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
    x, y = position
    layer.paste(overlay, (x, y), overlay)
    return Image.alpha_composite(white_bg, layer)


def _text_bbox(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> tuple[int, int]:
    if not text:
        return 0, 0
    display_text = shape_text(text)
    if hasattr(draw, "textbbox"):
        left, top, right, bottom = draw.textbbox((0, 0), display_text, font=font)
        return right - left, bottom - top
    width, height = draw.textsize(display_text, font=font)
    return width, height


def _truncate_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> str:
    if _text_bbox(draw, text, font)[0] <= max_width:
        return text
    ellipsis = "…"
    trimmed = text
    while trimmed:
        candidate = trimmed + ellipsis
        if _text_bbox(draw, candidate, font)[0] <= max_width:
            return candidate
        trimmed = trimmed[:-1]
    return ellipsis


def _wrap_title_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Smart text wrapping with better break positions."""
    import re
    
    words = text.split()
    if not words:
        return []

    # Terms that should not be split across lines
    unsplittable = {
        "4K", "LED", "RAM", "SSD", "GB", "TB", "Hz", "inch", "inches",
        "WiFi", "Bluetooth", "USB", "HDMI", "DPI", "FPS", "MP",
        "جيجا", "بوصة", "ميجا", "تيرا",
        "Pro", "Max", "Plus", "Ultra", "Lite", "Mini",
    }

    # Patterns for model identifiers (should not be split even if they contain separators)
    model_patterns = [
        r'[A-Z]{2,}\d{3,}[A-Z]*',  # SM-G991B, UA65M80H
        r'[A-Za-z]+\d+',  # iPhone16, M80H
        r'\d+[A-Za-z]+',  # 4060Ti
        r'USB-C',  # USB-C
        r'Type-C',  # Type-C
        r'Wi-Fi',  # Wi-Fi
        r'DDR\d+-\d+',  # DDR5-5600
        r'RTX-\d+',  # RTX-4060
    ]

    def is_model_identifier(word: str) -> bool:
        """Check if word is a model identifier that should not be split."""
        for pattern in model_patterns:
            if re.search(pattern, word, re.IGNORECASE):
                return True
        return False

    lines: list[str] = []
    current = ""
    
    for word in words:
        trial = f"{current} {word}".strip() if current else word
        trial_width = _text_bbox(draw, trial, font)[0]
        
        if trial_width <= max_width:
            current = trial
            continue
        
        # Word doesn't fit, start new line
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                break
            current = word
        else:
            # Single word too long
            # Check if it contains unsplittable term or is a model identifier
            word_upper = word.upper()
            is_unsplittable = any(term.upper() in word_upper for term in unsplittable)
            is_model = is_model_identifier(word)
            
            if is_unsplittable or is_model:
                # Truncate unsplittable/model word
                current = _truncate_line(draw, word, font, max_width)
                lines.append(current)
                if len(lines) >= max_lines:
                    break
                current = ""
            else:
                # Try to split at common separators (but not in model identifiers)
                separators = ['-', '/', '(', ')']
                split_done = False
                for sep in separators:
                    if sep in word and not is_model_identifier(word):
                        parts = word.split(sep)
                        temp_lines = []
                        temp_current = ""
                        for i, part in enumerate(parts):
                            test = temp_current + part if temp_current else part
                            if i < len(parts) - 1:
                                test += sep
                            if _text_bbox(draw, test, font)[0] <= max_width:
                                temp_current = test
                            else:
                                if temp_current:
                                    temp_lines.append(temp_current)
                                temp_current = part + (sep if i < len(parts) - 1 else "")
                        if temp_current:
                            temp_lines.append(temp_current)
                        
                        if len(temp_lines) > 1:
                            lines.extend(temp_lines[:max_lines - len(lines)])
                            if len(lines) >= max_lines:
                                break
                            current = ""
                            split_done = True
                            break
                
                if not split_done:
                    # Can't split, truncate
                    current = _truncate_line(draw, word, font, max_width)
                    lines.append(current)
                    if len(lines) >= max_lines:
                        break
                    current = ""

    if len(lines) < max_lines and current:
        lines.append(current)
    elif lines and len(lines) == max_lines:
        lines[-1] = _truncate_line(draw, lines[-1], font, max_width)

    return lines[:max_lines]


def _format_price_number(price_text: str) -> str:
    """Format numeric portion with thousands separators."""
    n = parse_price_number(price_text)
    if n is None:
        cleaned = price_text.strip()
        for token in ("جنيه", "EGP", "egp", "£"):
            cleaned = cleaned.replace(token, "").strip()
        return cleaned or price_text.strip()
    if abs(n - round(n)) < 0.01:
        return f"{int(round(n)):,}"
    text = f"{n:,.2f}".rstrip("0").rstrip(".")
    return text


def _parse_price_display(price_text: str) -> tuple[str, str]:
    """Return (formatted_number, currency_label)."""
    number = _format_price_number(price_text)
    currency = _CURRENCY_LABEL
    upper = price_text.upper()
    if "EGP" in upper and "جنيه" not in price_text:
        currency = "EGP"
    return number, currency


def _scaled(value: float, scale: float) -> int:
    return max(1, int(round(value * scale)))


def _panel_content_scale(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    list_price: str | None,
    panel_width: int,
    slot_height: int,
) -> float:
    """Pick a uniform scale so the centered info group fits inside the left panel."""
    for scale in (1.0, 0.92, 0.85, 0.78, 0.72, 0.66):
        if _panel_content_fits(
            draw,
            title=title,
            price=price,
            list_price=list_price,
            panel_width=panel_width,
            slot_height=slot_height,
            scale=scale,
        ):
            return scale
    return 0.66


def _panel_content_fits(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    list_price: str | None,
    panel_width: int,
    slot_height: int,
    scale: float,
) -> bool:
    available = slot_height - 2 * _INFO_PAD
    group_h = _info_content_group_height(
        draw,
        title=title,
        price=price,
        list_price=list_price,
        panel_width=panel_width,
        scale=scale,
    )
    return group_h <= available


def _measure_info_content_width(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    list_price: str | None,
    seller_name: str | None = None,
    panel_width: int,
    scale: float = 1.0,
) -> int:
    widths: list[int] = []
    if _valid_price(price):
        box_w, _ = _price_card_dimensions(draw, price.strip(), panel_width, scale)
        widths.append(box_w)
    if _valid_price(list_price):
        number, currency = _parse_price_display(list_price.strip())
        text = f"{_OLD_PRICE_LABEL} {number} {currency}"
        font = _load_ui_font(
            _scaled(_OLD_PRICE_FONT, scale),
            text,
            bold=False,
        )
        widths.append(_text_bbox(draw, text, font)[0])
    if title and title.strip() != "Not found":
        title_font, lines = _resolve_title_layout(
            draw, title.strip(), panel_width, scale
        )
        for line in lines:
            widths.append(_text_bbox(draw, line, title_font)[0])
    if seller_name and seller_name.strip():
        label_font_size = _scaled(int(_TITLE_FONT_MAX * 0.5), scale)
        name_font_size = _scaled(int(_TITLE_FONT_MAX * 0.6), scale)
        label_font = _load_ui_font(label_font_size, "Sold by", bold=False)
        name_font = _load_title_font(name_font_size, title=seller_name)
        if label_font and name_font:
            # Measure both lines
            label_bbox = draw.textbbox((0, 0), "Sold by", font=label_font)
            name_bbox = draw.textbbox((0, 0), seller_name, font=name_font)
            widths.append(label_bbox[2] - label_bbox[0])
            widths.append(name_bbox[2] - name_bbox[0])
    return max(widths) if widths else 0


def _title_block_height(
    draw: ImageDraw.ImageDraw,
    title: str | None,
    panel_width: int,
    scale: float,
) -> int:
    if not title or title.strip() == "Not found":
        return 0
    title_font, lines = _resolve_title_layout(draw, title.strip(), panel_width, scale)
    if not lines:
        return 0
    line_gap = _scaled(_TITLE_LINE_GAP, scale)
    height = 0
    for index, line in enumerate(lines):
        height += _text_bbox(draw, line, title_font)[1]
        if index < len(lines) - 1:
            height += line_gap
    return height


def _info_content_group_height(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    list_price: str | None,
    seller_name: str | None = None,
    panel_width: int,
    scale: float,
) -> int:
    total = 0
    has_title = bool(title and title.strip() != "Not found")
    has_old = _valid_price(list_price)
    has_current = _valid_price(price)
    has_seller = bool(seller_name and seller_name.strip())

    if has_title:
        total += _title_block_height(draw, title, panel_width, scale)
    if has_old:
        if has_title:
            total += _scaled(_TITLE_OLD_PRICE_GAP, scale)
        total += _old_price_block_height(draw, list_price.strip(), panel_width, scale)
    if has_current:
        if has_old:
            total += _scaled(_OLD_PRICE_CARD_GAP, scale)
        elif has_title:
            total += _scaled(_TITLE_OLD_PRICE_GAP, scale)
        _, box_h = _price_card_dimensions(draw, price.strip(), panel_width, scale)
        total += box_h
        if has_seller:
            total += _scaled(_PRICE_SELLER_GAP, scale)
            total += _seller_line_height(draw, seller_name.strip(), panel_width, scale)
    return total


def _format_resale_condition_display(seller_condition: str | None) -> str:
    """Format condition for display on Resale deal image card."""
    if not seller_condition:
        return "مستعمل"
    cond_upper = seller_condition.strip().upper()
    if cond_upper in ("USED", "USED - GENERIC", "مستعمل"):
        return "مستعمل"
    if "LIKE NEW" in cond_upper or "LIKENEW" in cond_upper or "شبه جديد" in seller_condition:
        return "مستعمل - شبه جديد"
    if "VERY GOOD" in cond_upper or "جيد جداً" in seller_condition or "جيد جدا" in seller_condition:
        return "مستعمل - بحالة جيدة جداً"
    if "GOOD" in cond_upper or "جيد" in seller_condition:
        return "مستعمل - بحالة جيدة"
    if "ACCEPTABLE" in cond_upper or "مقبول" in seller_condition:
        return "مستعمل - بحالة مقبولة"
    clean_cond = seller_condition.strip()
    if "مستعمل" not in clean_cond:
        return f"مستعمل - {clean_cond}"
    return clean_cond


def _seller_line_height(
    draw: ImageDraw.ImageDraw,
    seller_name: str,
    panel_width: int,
    scale: float,
    seller_condition: str | None = None,
) -> int:
    """Calculate height of seller badge with condition lines."""
    is_resale = "RESALE" in seller_name.upper() or "مستعمل" in seller_name
    font_size = _scaled(int(_TITLE_FONT_MAX * (0.51 if is_resale else 0.44)), scale)
    font_header = _load_title_font(font_size, title="Amazon Resale" if is_resale else "البائع: Amazon.eg")
    if font_header is None:
        return 0
    pad_y = _scaled(12 if is_resale else 10, scale)
    if is_resale:
        cond_text = _format_resale_condition_display(seller_condition)
        font_cond = _load_title_font(int(font_size * 0.9), title=cond_text)
        tb_h1 = _text_bbox(draw, "Amazon Resale", font_header)[1]
        tb_h2 = _text_bbox(draw, cond_text, font_cond)[1] if font_cond else 0
        gap_y = _scaled(5, scale)
        return tb_h1 + tb_h2 + gap_y + pad_y * 2
    return font_header.size + pad_y * 2


def draw_seller_badge(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    seller_name: str,
    price_card_rect: tuple[int, int, int, int],
    scale: float,
    seller_condition: str | None = None,
) -> int:
    """Draw a visually distinct seller badge below price card with condition support."""
    card_x, card_y, card_w, card_h = price_card_rect
    card_center_x = card_x + card_w // 2

    is_resale = "RESALE" in seller_name.upper() or "مستعمل" in seller_name

    if is_resale:
        header_text = "Amazon Resale"
        cond_text = _format_resale_condition_display(seller_condition)
        badge_fill = (240, 248, 242, 255)
        text_color = (20, 75, 30, 255)
        cond_color = (35, 115, 45, 255)
        border_color = (140, 195, 145, 255)
        font_size = _scaled(int(_TITLE_FONT_MAX * 0.51), scale)
        pad_x = _scaled(26, scale)
        pad_y = _scaled(12, scale)
        gap_y = _scaled(5, scale)
        radius = _scaled(16, scale)
    else:
        header_text = "البائع: Amazon.eg"
        cond_text = None
        badge_fill = (242, 246, 252, 255)
        text_color = (0, 69, 124, 255)
        cond_color = None
        border_color = (180, 205, 235, 255)
        font_size = _scaled(int(_TITLE_FONT_MAX * 0.44), scale)
        pad_x = _scaled(22, scale)
        pad_y = _scaled(10, scale)
        gap_y = _scaled(4, scale)
        radius = _scaled(14, scale)

    font_header = _load_title_font(font_size, title=header_text)
    font_cond = _load_title_font(int(font_size * 0.9), title=cond_text or "") if cond_text else None

    tb_w1, tb_h1 = _text_bbox(draw, header_text, font_header)
    tb_w2, tb_h2 = _text_bbox(draw, cond_text, font_cond) if (cond_text and font_cond) else (0, 0)

    box_w = max(tb_w1, tb_w2) + pad_x * 2
    box_h = tb_h1 + (tb_h2 + gap_y if cond_text else 0) + pad_y * 2

    x1 = card_center_x - box_w // 2
    x2 = x1 + box_w
    y1 = y
    y2 = y1 + box_h

    # Subtle ambient drop shadow
    shadow_y = max(1, _scaled(2, scale))
    draw.rounded_rectangle(
        (x1, y1 + shadow_y, x2, y2 + shadow_y),
        radius=radius,
        fill=(0, 0, 0, 14),
    )

    # Draw rounded rectangle badge with subtle border
    draw.rounded_rectangle(
        (x1, y1, x2, y2),
        radius=radius,
        fill=badge_fill,
        outline=border_color,
        width=max(1, _scaled(1.5, scale)),
    )

    header_x = x1 + (box_w - tb_w1) // 2
    _draw_text(draw, (header_x, y1 + pad_y), header_text, font_header, text_color)

    if cond_text and font_cond:
        cond_x = x1 + (box_w - tb_w2) // 2
        _draw_text(draw, (cond_x, y1 + pad_y + tb_h1 + gap_y), cond_text, font_cond, cond_color)

    return y2


def _resolve_title_layout(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int,
    scale: float,
) -> tuple[ImageFont.ImageFont, list[str]]:
    """Resolve title layout with width-based dynamic sizing."""
    max_size = _scaled(_TITLE_FONT_MAX, scale)
    min_size = max(20, _scaled(_TITLE_FONT_MIN, scale))
    step = max(2, _scaled(2, scale))
    
    # Try font sizes from largest to smallest
    for size in range(max_size, min_size - 1, -step):
        font = _load_title_font(size, title=title)
        lines = _wrap_title_lines(draw, title, font, max_width, _TITLE_MAX_LINES)
        if lines:
            # Check if the layout looks balanced
            # Prefer layouts that use both lines efficiently for longer titles
            if len(lines) == 2:
                # Check if second line is too short (orphan)
                line1_width = _text_bbox(draw, lines[0], font)[0]
                line2_width = _text_bbox(draw, lines[1], font)[0]
                # If second line is less than 30% of first line, try smaller font
                if line2_width < line1_width * 0.3 and size > min_size + step:
                    continue
            return font, lines
    
    # Fallback to minimum size
    font = _load_title_font(min_size, title=title)
    return font, _wrap_title_lines(draw, title, font, max_width, _TITLE_MAX_LINES)


def _fit_price_number_font(
    draw: ImageDraw.ImageDraw,
    number_text: str,
    max_width: int,
    scale: float,
) -> tuple[ImageFont.ImageFont, int]:
    inner_max = max_width - _scaled(_PRICE_CARD_INNER_RESERVE, scale)
    max_size = _scaled(_PRICE_NUM_MAX, scale)
    min_size = max(28, _scaled(_PRICE_NUM_MIN, scale))
    step = max(2, _scaled(2, scale))
    for size in range(max_size, min_size - 1, -step):
        font = _load_ui_font(size, number_text, bold=True)
        if _text_bbox(draw, number_text, font)[0] <= inner_max:
            return font, size
    font = _load_ui_font(min_size, number_text, bold=True)
    return font, min_size


class _PriceCardInnerLayout(NamedTuple):
    box_w: int
    box_h: int
    pad_x: int
    pad_y: int
    content_h: int
    label_draw_y: int
    num_draw_y: int
    curr_draw_y: int
    label_w: int
    num_w: int
    curr_w: int
    label_font: ImageFont.ImageFont
    num_font: ImageFont.ImageFont
    curr_font: ImageFont.ImageFont
    number: str
    currency: str
    radius: int


def _text_origin_bbox(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont
) -> tuple[int, int, int, int]:
    display_text = shape_text(text)
    if hasattr(draw, "textbbox"):
        return draw.textbbox((0, 0), display_text, font=font)
    width, height = draw.textsize(display_text, font=font)
    return 0, 0, width, height


def _stack_block_from_top(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    visual_top: int,
) -> tuple[int, int, int]:
    left, top, right, bottom = _text_origin_bbox(draw, text, font)
    draw_y = visual_top - top
    visual_bottom = draw_y + bottom
    return draw_y, visual_bottom, right - left


def _layout_price_card_inner(
    draw: ImageDraw.ImageDraw,
    price: str,
    panel_width: int,
    scale: float,
) -> _PriceCardInnerLayout:
    number, currency = _parse_price_display(price)
    num_font, _ = _fit_price_number_font(draw, number, panel_width, scale)
    # Use medium weight for label (semi-bold)
    label_font = _load_ui_font(_scaled(_PRICE_LABEL_FONT, scale), _PRICE_LABEL, bold=True)
    # Use bold weight for currency to increase contrast
    curr_font = _load_ui_font(_scaled(_PRICE_CURRENCY_FONT, scale), currency, bold=True)

    pad_x = _scaled(_PRICE_CARD_PAD_X, scale)
    pad_y = _scaled(_PRICE_CARD_PAD_Y, scale)
    label_gap = _scaled(6, scale)
    num_curr_gap = _scaled(_PRICE_NUM_CURRENCY_GAP, scale)
    radius = _scaled(_PRICE_CARD_RADIUS, scale)

    visual_top = 0
    label_draw_y, visual_top, label_w = _stack_block_from_top(
        draw, _PRICE_LABEL, label_font, visual_top
    )
    visual_top += label_gap
    num_draw_y, num_bottom, num_w = _stack_block_from_top(
        draw, number, num_font, visual_top
    )
    visual_top = num_bottom + num_curr_gap
    curr_draw_y, content_bottom, curr_w = _stack_block_from_top(
        draw, currency, curr_font, visual_top
    )
    content_h = content_bottom

    content_w = max(label_w, num_w, curr_w)
    natural_w = content_w + pad_x * 2
    box_w = int(natural_w * _PRICE_CARD_WIDTH_BOOST)
    box_h = content_h + pad_y * 2

    return _PriceCardInnerLayout(
        box_w=box_w,
        box_h=box_h,
        pad_x=pad_x,
        pad_y=pad_y,
        content_h=content_h,
        label_draw_y=label_draw_y,
        num_draw_y=num_draw_y,
        curr_draw_y=curr_draw_y,
        label_w=label_w,
        num_w=num_w,
        curr_w=curr_w,
        label_font=label_font,
        num_font=num_font,
        curr_font=curr_font,
        number=number,
        currency=currency,
        radius=radius,
    )


def _price_card_dimensions(
    draw: ImageDraw.ImageDraw,
    price: str,
    panel_width: int,
    scale: float,
) -> tuple[int, int]:
    layout = _layout_price_card_inner(draw, price, panel_width, scale)
    return layout.box_w, layout.box_h


def _old_price_block_height(
    draw: ImageDraw.ImageDraw,
    list_price: str,
    panel_width: int,
    scale: float,
) -> int:
    number, currency = _parse_price_display(list_price)
    text = f"{_OLD_PRICE_LABEL} {number} {currency}"
    font = _load_ui_font(_scaled(_OLD_PRICE_FONT, scale), text, bold=False)
    return _text_bbox(draw, text, font)[1]


def draw_title(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    title: str,
    panel_x: int,
    panel_width: int,
    scale: float,
    trailing_gap: bool = True,
) -> int:
    """Draw title with dynamic line spacing based on font size."""
    title_font, lines = _resolve_title_layout(draw, title, panel_width, scale)
    
    # Calculate dynamic line spacing (22% of font size)
    font_size = title_font.size if hasattr(title_font, 'size') else int(_scaled(_TITLE_FONT_MAX, scale))
    line_gap = int(font_size * 0.22)
    
    current_y = y
    for line in lines:
        line_h = _draw_aligned_text(
            draw,
            panel_x,
            current_y,
            line,
            title_font,
            _BLACK_TEXT,
            panel_x,
            panel_width,
            _contains_arabic(line),
        )
        current_y += line_h + line_gap
    
    if lines:
        current_y -= line_gap
    if trailing_gap:
        return current_y + _scaled(_TITLE_AFTER_GAP, scale)
    return current_y
def draw_old_price(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    list_price: str,
    panel_x: int,
    panel_width: int,
    rtl: bool,
    scale: float,
) -> int:
    number, currency = _parse_price_display(list_price)

    label = _OLD_PRICE_LABEL
    number_text = number
    currency_text = currency

    font = _load_ui_font(
        _scaled(_OLD_PRICE_FONT, scale),
        f"{label} {number_text} {currency_text}",
        bold=False,
    )

    label_w, label_h = _text_bbox(draw, label, font)
    space_w, _ = _text_bbox(draw, " ", font)
    number_w, number_h = _text_bbox(draw, number_text, font)
    currency_w, currency_h = _text_bbox(draw, currency_text, font)

    total_w = (
        label_w
        + space_w
        + number_w
        + space_w
        + currency_w
    )

    draw_x = panel_x + panel_width - total_w if rtl else panel_x

    if rtl:
        # Arabic RTL layout
        currency_x = draw_x
        number_x = currency_x + currency_w + space_w
        label_x = number_x + number_w + space_w
    else:
        label_x = draw_x
        number_x = label_x + label_w + space_w
        currency_x = number_x + number_w + space_w

    _draw_text(draw, (label_x, y), label, font, _GRAY_TEXT)
    _draw_text(draw, (number_x, y), number_text, font, _GRAY_TEXT)
    _draw_text(draw, (currency_x, y), currency_text, font, _GRAY_TEXT)

    # Exact strikethrough alignment through vertical center of digits
    num_disp = shape_text(number_text)
    if hasattr(draw, "textbbox"):
        nb = draw.textbbox((0, 0), num_disp, font=font)
        strike_y = y + (nb[1] + nb[3]) // 2
    else:
        strike_y = y + number_h // 2

    strike_pad = _scaled(2, scale)
    draw.line(
        (
            number_x - strike_pad,
            strike_y,
            number_x + number_w + strike_pad,
            strike_y,
        ),
        fill=_GRAY_TEXT,
        width=max(2, _scaled(_OLD_PRICE_STRIKE_WIDTH, scale)),
    )

    return y + max(label_h, number_h, currency_h)

def draw_price_card(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    price: str,
    panel_x: int,
    panel_width: int,
    rtl: bool,
    scale: float,
) -> tuple[int, tuple[int, int, int, int]]:
    layout = _layout_price_card_inner(draw, price, panel_width, scale)

    if rtl:
        box_x2 = panel_x + panel_width
        box_x1 = box_x2 - layout.box_w
    else:
        box_x1 = panel_x
        box_x2 = box_x1 + layout.box_w
    box_y1 = y
    box_y2 = box_y1 + layout.box_h

    # Soft ambient drop shadow for subtle depth
    shadow_y = max(2, _scaled(4, scale))
    draw.rounded_rectangle(
        (box_x1 + 1, box_y1 + shadow_y, box_x2 + 1, box_y2 + shadow_y),
        radius=layout.radius,
        fill=(0, 0, 0, 18),
    )

    # Main yellow card with crisp border
    draw.rounded_rectangle(
        (box_x1, box_y1, box_x2, box_y2),
        radius=layout.radius,
        fill=_AMAZON_YELLOW,
        outline=_AMAZON_YELLOW_BORDER,
        width=max(1, _scaled(1, scale)),
    )

    inner_h = layout.box_h - layout.pad_y * 2
    content_offset = max(0, (inner_h - layout.content_h) // 2)
    base_y = box_y1 + layout.pad_y + content_offset
    inner_x = box_x2 - layout.pad_x if rtl else box_x1 + layout.pad_x

    label_x = inner_x - layout.label_w if rtl else inner_x
    _draw_text(
        draw,
        (label_x, base_y + layout.label_draw_y),
        _PRICE_LABEL,
        layout.label_font,
        _LABEL_GRAY,
    )

    num_x = inner_x - layout.num_w if rtl else inner_x
    _draw_text(
        draw,
        (num_x, base_y + layout.num_draw_y),
        layout.number,
        layout.num_font,
        _BLACK_TEXT,
    )

    curr_x = inner_x - layout.curr_w if rtl else inner_x
    _draw_text(
        draw,
        (curr_x, base_y + layout.curr_draw_y),
        layout.currency,
        layout.curr_font,
        _BLACK_TEXT,
    )

    return (box_y2, (box_x1, box_y1, layout.box_w, layout.box_h))


def draw_discount_badge(
    canvas: Image.Image,
    *,
    price: str | None,
    list_price: str | None,
    scale: float = 1.0,
) -> int | None:
    """Draw top-right discount badge; return its bottom y."""
    discount_text = _discount_badge_text(price, list_price)
    if not discount_text:
        return None

    draw = ImageDraw.Draw(canvas)
    font = _load_ui_font(_scaled(_DISCOUNT_BADGE_FONT, scale), discount_text, bold=True)
    text_w, text_h = _text_bbox(draw, discount_text, font)
    pad_x = _scaled(_DISCOUNT_BADGE_PAD_X, scale)
    pad_y = _scaled(_DISCOUNT_BADGE_PAD_Y, scale)
    box_w = text_w + pad_x * 2
    badge_x = canvas.width - _CORNER_BADGE_MARGIN
    x1 = badge_x - box_w
    y1 = _CORNER_BADGE_MARGIN
    _draw_pill_badge(
        canvas,
        discount_text,
        x1,
        y1,
        font=font,
        pad_x=pad_x,
        pad_y=pad_y,
        radius=_scaled(_DISCOUNT_BADGE_RADIUS, scale),
        fill=_DISCOUNT_RED,
        text_color=(255, 255, 255, 255),
    )
    return y1 + text_h + pad_y * 2


def draw_prime_badge(
    canvas: Image.Image,
    *,
    anchor_y: int,
    scale: float = 1.0,
) -> None:
    draw = ImageDraw.Draw(canvas)
    text = "prime"
    font = _load_ui_font(_scaled(_PRIME_BADGE_FONT, scale), text, bold=True)
    text_w, _ = _text_bbox(draw, text, font)
    pad_x = _scaled(_PRIME_BADGE_PAD_X, scale)
    pad_y = _scaled(_PRIME_BADGE_PAD_Y, scale)
    box_w = text_w + pad_x * 2
    badge_x = canvas.width - _CORNER_BADGE_MARGIN
    x1 = badge_x - box_w
    _draw_pill_badge(
        canvas,
        text,
        x1,
        anchor_y,
        font=font,
        pad_x=pad_x,
        pad_y=pad_y,
        radius=_scaled(_PRIME_BADGE_RADIUS, scale),
        fill=_PRIME_BLUE_LIGHT,
        text_color=(255, 255, 255, 255),
    )


def _draw_aligned_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int, int],
    panel_x: int,
    panel_width: int,
    rtl: bool,
) -> int:
    tw, th = _text_bbox(draw, text, font)
    draw_x = panel_x + panel_width - tw if rtl else x
    _draw_text(draw, (draw_x, y), text, font, fill)
    return th


def _draw_info_panel(
    canvas: Image.Image,
    *,
    left_w: int,
    slot_height: int,
    title: str | None,
    price: str | None,
    list_price: str | None,
    seller_name: str | None = None,
    seller_condition: str | None = None,
    layout_scale: float,
) -> None:
    draw = ImageDraw.Draw(canvas)
    panel_x = _INFO_PAD
    panel_width = left_w - 2 * _INFO_PAD

    group_h = _info_content_group_height(
        draw,
        title=title,
        price=price,
        list_price=list_price,
        seller_name=seller_name,
        panel_width=panel_width,
        scale=layout_scale,
    )
    if group_h <= 0:
        return

    available = slot_height - 2 * _INFO_PAD
    cursor_y = _INFO_PAD + max(0, (available - group_h) // 2)

    has_title = bool(title and title.strip() != "Not found")
    has_old = _valid_price(list_price)
    has_current = _valid_price(price)
    has_seller = bool(seller_name and seller_name.strip())

    if has_title:
        cursor_y = draw_title(
            draw,
            y=cursor_y,
            title=title.strip(),
            panel_x=panel_x,
            panel_width=panel_width,
            scale=layout_scale,
            trailing_gap=False,
        )

    if has_old:
        if has_title:
            cursor_y += _scaled(_TITLE_OLD_PRICE_GAP, layout_scale)
        cursor_y = draw_old_price(
            draw,
            y=cursor_y,
            list_price=list_price.strip(),
            panel_x=panel_x,
            panel_width=panel_width,
            rtl=True,
            scale=layout_scale,
        )

    if has_current:
        if has_old:
            cursor_y += _scaled(_OLD_PRICE_CARD_GAP, layout_scale)
        elif has_title:
            cursor_y += _scaled(_TITLE_OLD_PRICE_GAP, layout_scale)
        price_card_y = cursor_y
        cursor_y, price_card_rect = draw_price_card(
            draw,
            y=cursor_y,
            price=price.strip(),
            panel_x=panel_x,
            panel_width=panel_width,
            rtl=True,
            scale=layout_scale,
        )

    # Draw seller name below price card (outside the price card)
    if has_seller and has_current:
        cursor_y += _scaled(_PRICE_SELLER_GAP, layout_scale)
        cursor_y = draw_seller_badge(
            draw,
            y=cursor_y,
            seller_name=seller_name.strip(),
            price_card_rect=price_card_rect,
            scale=layout_scale,
            seller_condition=seller_condition,
        )


def _discount_percent(price: str, list_price: str) -> int | None:
    price_n = parse_price_number(price)
    list_n = parse_price_number(list_price)
    if price_n is None or list_n is None or list_n <= 0:
        return None
    discount = round((list_n - price_n) / list_n * 100)
    return discount if discount > 0 else None


def _discount_badge_text(price: str | None, list_price: str | None) -> str | None:
    if not _valid_price(price) or not _valid_price(list_price):
        return None
    discount = _discount_percent(price, list_price)
    if discount is None:
        return None
    return f"خصم {discount}%"


def _draw_pill_badge(
    canvas: Image.Image,
    text: str,
    anchor_x: int,
    anchor_y: int,
    *,
    font: ImageFont.ImageFont,
    pad_x: int,
    pad_y: int,
    radius: int,
    fill: tuple[int, int, int, int],
    text_color: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    draw = ImageDraw.Draw(canvas)
    text_w, text_h = _text_bbox(draw, text, font)
    box_w = text_w + pad_x * 2
    box_h = text_h + pad_y * 2
    x1, y1 = anchor_x, anchor_y
    x2, y2 = x1 + box_w, y1 + box_h

    # Subtle drop shadow
    draw.rounded_rectangle(
        (x1 + 1, y1 + 2, x2 + 1, y2 + 3),
        radius=radius,
        fill=(0, 0, 0, 20),
    )

    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill)
    
    # Exact vertical centering
    t_bbox = _text_origin_bbox(draw, text, font)
    actual_t_h = t_bbox[3] - t_bbox[1]
    ty = y1 + (box_h - actual_t_h) // 2 - t_bbox[1]
    tx = x1 + (box_w - text_w) // 2

    _draw_text(draw, (tx, ty), text, font, text_color)
    return x1, y1, x2, y2


def _draw_corner_badges(
    canvas: Image.Image,
    *,
    price: str | None,
    list_price: str | None,
    prime_exclusive: bool,
    layout_scale: float,
) -> None:
    discount_bottom = draw_discount_badge(
        canvas,
        price=price,
        list_price=list_price,
        scale=layout_scale,
    )

    if prime_exclusive and discount_bottom is not None:
        draw_prime_badge(
            canvas,
            anchor_y=discount_bottom + _scaled(9, layout_scale),
            scale=layout_scale,
        )
    elif prime_exclusive:
        draw_prime_badge(
            canvas,
            anchor_y=_CORNER_BADGE_MARGIN,
            scale=layout_scale,
        )


def _render_card_product_image(
    image_path: str,
    area_w: int,
    area_h: int,
) -> Image.Image:
    image = _neutralize_transparent_rgb(
        _trim_product_borders(Image.open(image_path))
    )
    trimmed_w, trimmed_h = image.size
    aspect = trimmed_w / trimmed_h if trimmed_h else 1.0
    scale = _compute_product_scale(trimmed_w, trimmed_h, area_w, area_h)
    scaled_w = max(1, int(trimmed_w * scale))
    scaled_h = max(1, int(trimmed_h * scale))
    image_scaled = image.resize((scaled_w, scaled_h), Image.Resampling.LANCZOS)
    rel_x, rel_y = _compute_product_position(
        area_w, area_h, scaled_w, scaled_h, aspect
    )
    return _composite_on_white((area_w, area_h), image_scaled, (rel_x, rel_y))


def _draw_star_rating(draw: ImageDraw.ImageDraw, x: int, y: int, num_stars: int = 5, star_size: int = 14) -> int:
    """Draw 5 crisp gold star shapes without font dependency."""
    import math
    for s in range(num_stars):
        cx = x + s * (star_size + 3) + star_size / 2
        cy = y + star_size / 2
        points = []
        r_outer = star_size / 2
        r_inner = r_outer * 0.4
        for i in range(10):
            r = r_outer if i % 2 == 0 else r_inner
            angle = -math.pi / 2 + i * math.pi / 5
            px = cx + r * math.cos(angle)
            py = cy + r * math.sin(angle)
            points.append((px, py))
        draw.polygon(points, fill=(255, 164, 28, 255))
    return num_stars * (star_size + 3)


def _draw_prime_inline(draw: ImageDraw.ImageDraw, x: int, y: int) -> int:
    """Draw Amazon ✓ prime logo with orange checkmark and cyan prime text."""
    check_pts = [(x, y + 8), (x + 4, y + 12), (x + 11, y + 3)]
    draw.line(check_pts, fill=(255, 153, 0, 255), width=3)

    font = _load_title_font(18, title="prime")
    draw.text((x + 15, y - 2), "prime", font=font, fill=(0, 168, 225, 255))
    return 15 + _text_bbox(draw, "prime", font)[0]


def _draw_horizontal_composite_card(
    canvas: Image.Image,
    x: int,
    y: int,
    w: int,
    h: int,
    product: CreatorsProductCard,
) -> None:
    """Draw a single Amazon product card exactly matching reference layout."""
    draw = ImageDraw.Draw(canvas)

    # 1. Card Container (White card with soft shadow and subtle border)
    # Shadow
    draw.rounded_rectangle(
        (x + 2, y + 4, x + w + 2, y + h + 4),
        radius=14,
        fill=(0, 0, 0, 10),
    )
    # White card fill & light gray border
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=14,
        fill=(255, 255, 255, 255),
        outline=(224, 228, 232, 255),
        width=1,
    )

    pad_x = 16
    inner_x = x + pad_x
    inner_w = w - pad_x * 2

    # 2. Product Image Area (Top section of card)
    image_h = 230
    if product.image_path and os.path.exists(product.image_path):
        img_area = _render_card_product_image(product.image_path, inner_w, image_h)
        canvas.paste(img_area, (inner_x, y + 16))

    cursor_y = y + 16 + image_h + 12

    # 3. Arabic Title Area (Bolder, significantly larger, up to 4 lines, RTL right-aligned)
    title_font_size = 22
    title_font = _load_title_font(title_font_size, title=product.title)

    lines: list[str] = []
    if product.title and product.title.strip() != "Not found":
        lines = _wrap_title_lines(draw, product.title.strip(), title_font, inner_w, max_lines=4)

    title_start_y = cursor_y
    line_gap = 4
    for line in lines:
        display_line = shape_text(line)
        bbox = draw.textbbox((0, 0), display_line, font=title_font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        line_x = inner_x + inner_w - text_w
        draw.text((line_x, cursor_y), display_line, font=title_font, fill=(15, 17, 17, 255))
        cursor_y += text_h + line_gap

    # Reserve uniform title block height so lower elements align across all cards
    title_block_h = 108
    cursor_y = title_start_y + title_block_h

    # 4. Rating Line ((1) 5.0 + 5 Gold Stars)
    rating_font = _load_ui_font(14, "(1) 5.0", bold=False)
    disp_rating = shape_text("(1) 5.0")
    r_bbox = draw.textbbox((0, 0), disp_rating, font=rating_font)
    r_w = r_bbox[2] - r_bbox[0]

    stars_w = 5 * (14 + 3)
    total_rating_w = r_w + 6 + stars_w
    rating_x = inner_x + inner_w - total_rating_w

    # Draw score text in slate gray #007185
    draw.text((rating_x + stars_w + 6, cursor_y), disp_rating, font=rating_font, fill=(0, 113, 133, 255))
    # Draw 5 crisp gold star shapes
    _draw_star_rating(draw, rating_x, cursor_y + 1, num_stars=5, star_size=14)

    cursor_y += 24

    # 5. Price Line (Large bold number + stacked/adjacent currency)
    price_str = product.price if _valid_price(product.price) else "0"
    num_str, curr_str = _parse_price_display(price_str)

    price_num_font = _load_title_font(34, title=num_str)
    price_curr_font = _load_ui_font(18, curr_str, bold=True)

    disp_num = shape_text(num_str)
    disp_curr = shape_text(curr_str)

    num_bbox = draw.textbbox((0, 0), disp_num, font=price_num_font)
    curr_bbox = draw.textbbox((0, 0), disp_curr, font=price_curr_font)

    num_w = num_bbox[2] - num_bbox[0]
    curr_w = curr_bbox[2] - curr_bbox[0]

    total_price_w = num_w + 6 + curr_w
    price_x = inner_x + inner_w - total_price_w

    draw.text((price_x + curr_w + 6, cursor_y), disp_num, font=price_num_font, fill=(15, 17, 17, 255))
    draw.text((price_x, cursor_y + 10), disp_curr, font=price_curr_font, fill=(86, 89, 89, 255))

    cursor_y += 44

    # 6. Prime & Delivery Badges (Cyan غداً badge + ✓ prime + delivery text)
    cyan_badge_w = 42
    cyan_badge_h = 22
    badge_x = inner_x + inner_w - cyan_badge_w
    draw.rounded_rectangle(
        (badge_x, cursor_y, badge_x + cyan_badge_w, cursor_y + cyan_badge_h),
        radius=4,
        fill=(0, 168, 225, 255),
    )
    gadan_font = _load_ui_font(13, "غداً", bold=True)
    disp_gadan = shape_text("غداً")
    gb_box = draw.textbbox((0, 0), disp_gadan, font=gadan_font)
    g_w = gb_box[2] - gb_box[0]
    draw.text((badge_x + (cyan_badge_w - g_w) // 2, cursor_y + 2), disp_gadan, font=gadan_font, fill=(255, 255, 255, 255))

    # ✓ prime logo next to badge
    prime_x = badge_x - 72
    _draw_prime_inline(draw, prime_x, cursor_y + 3)

    cursor_y += 28

    # Delivery info line
    deliv_font = _load_ui_font(14, "توصيل مجاني غداً، 18 أغسطس", bold=False)
    deliv_text = "توصيل مجاني غداً، 18 أغسطس"
    disp_deliv = shape_text(deliv_text)
    d_bbox = draw.textbbox((0, 0), disp_deliv, font=deliv_font)
    d_w = d_bbox[2] - d_bbox[0]
    draw.text((inner_x + inner_w - d_w, cursor_y), disp_deliv, font=deliv_font, fill=(86, 89, 89, 255))

    # 7. Add to Cart Button (Yellow CTA button at bottom of card)
    btn_h = 42
    btn_y = y + h - btn_h - 14
    draw.rounded_rectangle(
        (inner_x, btn_y, inner_x + inner_w, btn_y + btn_h),
        radius=12,
        fill=(255, 216, 20, 255),
    )
    btn_font = _load_title_font(16, title="إضافة إلى عربة التسوق")
    disp_btn = shape_text("إضافة إلى عربة التسوق")
    btn_bbox = draw.textbbox((0, 0), disp_btn, font=btn_font)
    bw = btn_bbox[2] - btn_bbox[0]
    bh = btn_bbox[3] - btn_bbox[1]
    btn_tx = inner_x + (inner_w - bw) // 2
    btn_ty = btn_y + (btn_h - bh) // 2 - 2
    draw.text((btn_tx, btn_ty), disp_btn, font=btn_font, fill=(15, 17, 17, 255))


def _apply_frame_creators_composite(
    output_path: str,
    products: list[CreatorsProductCard],
) -> str:
    """Render 2–5 products in a clean side-by-side horizontal card layout matching reference screenshot."""
    count = len(products)
    card_w = 330
    card_h = 680
    gap = 18
    pad_x = 28
    pad_y = 28
    top_header_h = 40

    canvas_w = pad_x * 2 + count * card_w + (count - 1) * gap
    canvas_h = pad_y * 2 + top_header_h + card_h

    # Light background matching reference #F3F4F7
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (243, 244, 247, 255))

    # Draw Brand Logo in top-left corner if available
    try:
        frame_path = "frame.png"
        if os.path.exists(frame_path):
            frame_img = Image.open(frame_path).convert("RGBA")
            # Extract small brand logo from top-left if available or draw green circle badge
            draw_top = ImageDraw.Draw(canvas)
            draw_top.ellipse((pad_x, pad_y - 8, pad_x + 36, pad_y + 28), fill=(40, 167, 69, 255))
            draw_top.ellipse((pad_x + 8, pad_y, pad_x + 28, pad_y + 20), fill=(255, 255, 255, 255))
    except Exception:
        pass

    start_y = pad_y + top_header_h

    # Render each product card horizontally side-by-side
    for idx, product in enumerate(products):
        card_x = pad_x + idx * (card_w + gap)
        _draw_horizontal_composite_card(canvas, card_x, start_y, card_w, card_h, product)

    canvas.convert("RGB").save(output_path, quality=95)
    return output_path


def apply_frame_creators_products(
    output_path: str,
    products: list[CreatorsProductCard],
) -> str:
    """
    Frame one to six Creators API products.

    Single product uses the existing premium layout unchanged.
    Two to six products automatically use the composite grid layout.
    """
    if not products:
        raise ValueError("At least one product is required")
    if len(products) > _COMPOSITE_MAX_PRODUCTS:
        raise ValueError(
            f"Composite layout supports at most {_COMPOSITE_MAX_PRODUCTS} products"
        )

    if len(products) == 1:
        product = products[0]
        return apply_frame_creators_product(
            product.image_path,
            output_path,
            title=product.title,
            price=product.price,
            list_price=product.list_price,
            prime_exclusive=product.prime_exclusive,
            seller_name=product.seller_name,
        )

    return _apply_frame_creators_composite(output_path, products)


def apply_frame_top_aligned(image_path, output_path="framed_custom.png"):
    """
    Apply frame to custom image with top-aligned fitting behavior.

    Frame and slot dimensions are derived from frame.png via get_frame_geometry().

    Image fitting rules:
    - Image is always top-aligned within the slot.
    - Aspect ratio is preserved.
    - Use COVER behavior: scale so slot is always fully occupied vertically.
    - If scaled image exceeds slot dimensions, crop from BOTTOM only.
    - Never center vertically.
    """
    frame_path = "frame.png"

    frame = Image.open(frame_path).convert("RGBA")
    geo = get_frame_geometry(frame)
    image = Image.open(image_path).convert("RGBA")

    # Calculate scaling using COVER behavior
    # Scale so that BOTH dimensions are at least as large as the slot
    original_width, original_height = image.size
    scale_width = geo.slot_width / original_width
    scale_height = geo.slot_height / original_height
    scale_factor = max(scale_width, scale_height)
    scaled_width = int(original_width * scale_factor)
    scaled_height = int(original_height * scale_factor)

    # Resize image using COVER scale
    image_scaled = image.resize((scaled_width, scaled_height), Image.Resampling.LANCZOS)

    # Create canvas at slot dimensions with white background
    canvas = Image.new("RGBA", (geo.slot_width, geo.slot_height), (255, 255, 255, 255))

    # Paste at top (y=0), crop excess from bottom
    # Since we used COVER, scaled_width >= slot_width and scaled_height >= slot_height
    # We need to crop from left/right if width exceeds, and from bottom if height exceeds
    crop_left = (scaled_width - geo.slot_width) // 2
    crop_top = 0  # Always align to top
    crop_right = crop_left + geo.slot_width
    crop_bottom = geo.slot_height  # Crop from bottom only

    image_cropped = image_scaled.crop((crop_left, crop_top, crop_right, crop_bottom))
    canvas.paste(image_cropped, (0, 0))

    # Create final canvas at frame dimensions
    final_canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    final_canvas.paste(canvas, (geo.slot_x, geo.slot_y))

    # Composite the frame on top
    final = Image.alpha_composite(final_canvas, frame)

    final.save(output_path)

    return output_path
