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
_LEFT_PANEL_RATIO_MIN = 0.30
_LEFT_PANEL_RATIO_MAX = 0.32
_OLD_PRICE_CARD_GAP = 24
_TITLE_OLD_PRICE_GAP = 32
_IMG_PANEL_PAD = 16
_IMG_FILL_RATIO = 0.966
_TALL_ASPECT_THRESHOLD = 0.88
_INFO_PAD = 28
_INFO_TOP_OFFSET = 6
_TITLE_MAX_LINES = 2
_TITLE_AFTER_GAP = 20
_TITLE_FONT_MAX = int(72 * _FONT_SCALE)
_TITLE_FONT_MIN = int(44 * _FONT_SCALE)
_TITLE_LINE_GAP = int(16 * _FONT_SCALE)
_OLD_PRICE_FONT = int(70 * _FONT_SCALE)
_OLD_PRICE_STRIKE_WIDTH = int(6 * _FONT_SCALE)
_PRICE_LABEL_FONT = int(32 * _FONT_SCALE)
_PRICE_CURRENCY_FONT = int(42 * _FONT_SCALE)
_PRICE_NUM_MAX = int(110 * _FONT_SCALE)
_PRICE_NUM_MIN = int(72 * _FONT_SCALE)
_PRICE_CARD_PAD_X = 50
_PRICE_CARD_PAD_Y = 35
_PRICE_CARD_RADIUS = 26
_PRICE_CARD_INNER_RESERVE = 100
_PRICE_CARD_WIDTH_BOOST = 1.12
_PRICE_NUM_CURRENCY_GAP = 11
_DISCOUNT_BADGE_FONT = int(46 * _FONT_SCALE)
_DISCOUNT_BADGE_PAD_X = 28
_DISCOUNT_BADGE_PAD_Y = 16
_DISCOUNT_BADGE_RADIUS = 19
_AMAZON_YELLOW = (255, 216, 20, 255)
_GRAY_TEXT = (70, 70, 70, 255)
_LABEL_GRAY = (85, 85, 85, 255)
_BLACK_TEXT = (10, 10, 10, 255)
_DISCOUNT_RED = (190, 35, 35, 255)
_PRIME_BLUE_LIGHT = (0, 168, 225, 105)
_PRIME_BADGE_FONT = int(26 * _FONT_SCALE)
_PRIME_BADGE_PAD_X = 17
_PRIME_BADGE_PAD_Y = 7
_PRIME_BADGE_RADIUS = 13
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
) -> int:
    """Size the info panel to content, clamped to ~30–32% of slot width."""
    min_w, max_w = _left_panel_width_bounds(slot_width)
    trial_inner = max_w - 2 * _INFO_PAD
    content_w = _measure_info_content_width(
        draw,
        title=title,
        price=price,
        list_price=list_price,
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
    title: str | None = None,
    price: str | None = None,
    list_price: str | None = None,
    prime_exclusive: bool = False,
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
    panel_width: int,
    scale: float,
) -> int:
    total = 0
    has_title = bool(title and title.strip() != "Not found")
    has_old = _valid_price(list_price)
    has_current = _valid_price(price)

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
    return total


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

    strike_y = y + number_h // 2 + _scaled(20, scale)

    draw.line(
        (
            number_x,
            strike_y,
            number_x + number_w,
            strike_y,
        ),
        fill=_GRAY_TEXT,
        width=max(1, _scaled(_OLD_PRICE_STRIKE_WIDTH, scale)),
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
) -> int:
    layout = _layout_price_card_inner(draw, price, panel_width, scale)

    if rtl:
        box_x2 = panel_x + panel_width
        box_x1 = box_x2 - layout.box_w
    else:
        box_x1 = panel_x
        box_x2 = box_x1 + layout.box_w
    box_y1 = y
    box_y2 = box_y1 + layout.box_h

    draw.rounded_rectangle(
        (box_x1, box_y1, box_x2, box_y2),
        radius=layout.radius,
        fill=_AMAZON_YELLOW,
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

    return box_y2


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
    font = _load_ui_font(_scaled(_DISCOUNT_BADGE_FONT, scale), discount_text, bold=False)
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
    font = _load_ui_font(_scaled(_PRIME_BADGE_FONT, scale), text, bold=False)
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
        text_color=(255, 255, 255, 220),
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
        draw_price_card(
            draw,
            y=cursor_y,
            price=price.strip(),
            panel_x=panel_x,
            panel_width=panel_width,
            rtl=True,
            scale=layout_scale,
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
    return f"🔥 خصم {discount}%"


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
    draw.rounded_rectangle((x1, y1, x2, y2), radius=radius, fill=fill)
    _draw_text(draw, (x1 + pad_x, y1 + pad_y), text, font, text_color)
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


def _composite_grid_metrics(
    count: int,
    slot_width: int,
    slot_height: int,
) -> tuple[int, int, int, int]:
    """Return (card_w, card_h, portrait_h, outer_pad) for the composite grid."""
    outer = _COMPOSITE_OUTER_PAD
    gap = _COMPOSITE_GRID_GAP
    inner_w = slot_width - outer * 2
    inner_h = slot_height - outer * 2
    card_w = (inner_w - gap) // 2

    if count == 2:
        return card_w, inner_h, 0, outer

    if count == 3:
        card_h = (inner_h - gap) // 2
        return card_w, card_h, 0, outer

    if count in (4, 6):
        rows = 2 if count == 4 else 3
        card_h = (inner_h - gap * (rows - 1)) // rows
        return card_w, card_h, 0, outer

    boost = _COMPOSITE_PORTRAIT_HEIGHT_RATIO
    card_h = max(1, int((inner_h - gap * 2) / (2 + boost)))
    portrait_h = max(1, int(card_h * boost))
    return card_w, card_h, portrait_h, outer


def _composite_card_slots(
    count: int,
    slot_width: int,
    slot_height: int,
) -> list[CompositeCardSlot]:
    """Compute pixel rectangles for each composite card."""
    card_w, card_h, portrait_h, outer = _composite_grid_metrics(
        count, slot_width, slot_height
    )
    gap = _COMPOSITE_GRID_GAP
    slots: list[CompositeCardSlot] = []

    if count == 2:
        y = outer
        slots.append(CompositeCardSlot(outer, y, card_w, card_h))
        slots.append(CompositeCardSlot(outer + card_w + gap, y, card_w, card_h))
        return slots

    if count == 3:
        y0 = outer
        slots.append(CompositeCardSlot(outer, y0, card_w, card_h))
        slots.append(CompositeCardSlot(outer + card_w + gap, y0, card_w, card_h))
        y1 = outer + card_h + gap
        centered_x = outer + (slot_width - outer * 2 - card_w) // 2
        slots.append(CompositeCardSlot(centered_x, y1, card_w, card_h))
        return slots

    if count in (4, 5):
        positions = (
            (outer, outer),
            (outer + card_w + gap, outer),
            (outer, outer + card_h + gap),
            (outer + card_w + gap, outer + card_h + gap),
        )
        for x, y in positions:
            slots.append(CompositeCardSlot(x, y, card_w, card_h))

    if count == 6:
        for row in range(3):
            y = outer + row * (card_h + gap)
            slots.append(CompositeCardSlot(outer, y, card_w, card_h))
            slots.append(CompositeCardSlot(outer + card_w + gap, y, card_w, card_h))
        return slots

    if count == 5:
        grid_bottom = outer + card_h * 2 + gap
        portrait_y = grid_bottom + gap
        centered_x = outer + (slot_width - outer * 2 - card_w) // 2
        slots.append(
            CompositeCardSlot(
                centered_x,
                portrait_y,
                card_w,
                portrait_h,
                portrait=True,
            )
        )

    return slots


def _resolve_composite_title_layout(
    draw: ImageDraw.ImageDraw,
    title: str,
    max_width: int,
    scale: float,
) -> tuple[ImageFont.ImageFont, list[str]]:
    max_size = _scaled(_COMPOSITE_TITLE_FONT_MAX, scale)
    min_size = max(12, _scaled(_COMPOSITE_TITLE_FONT_MIN, scale))
    step = max(2, _scaled(2, scale))
    for size in range(max_size, min_size - 1, -step):
        font = _load_title_font(size, title=title)
        lines = _wrap_title_lines(draw, title, font, max_width, _TITLE_MAX_LINES)
        if lines:
            return font, lines
    font = _load_title_font(min_size, title=title)
    return font, _wrap_title_lines(draw, title, font, max_width, _TITLE_MAX_LINES)


def _composite_title_block_height(
    draw: ImageDraw.ImageDraw,
    title: str | None,
    max_width: int,
    scale: float,
) -> int:
    if not title or title.strip() == "Not found":
        return 0
    title_font, lines = _resolve_composite_title_layout(
        draw, title.strip(), max_width, scale
    )
    if not lines:
        return 0
    line_gap = _scaled(_TITLE_LINE_GAP, scale)
    height = 0
    for index, line in enumerate(lines):
        height += _text_bbox(draw, line, title_font)[1]
        if index < len(lines) - 1:
            height += line_gap
    return height


def _composite_price_block_height(
    draw: ImageDraw.ImageDraw,
    price: str | None,
    max_width: int,
    scale: float,
) -> int:
    if not _valid_price(price):
        return 0
    _, box_h = _price_card_dimensions(draw, price.strip(), max_width, scale)
    return box_h


def _composite_card_text_height(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    inner_width: int,
    scale: float,
) -> int:
    total = 0
    title_h = _composite_title_block_height(draw, title, inner_width, scale)
    price_h = _composite_price_block_height(draw, price, inner_width, scale)
    if title_h:
        total += title_h
    if price_h:
        if title_h:
            total += _scaled(_COMPOSITE_TITLE_PRICE_GAP, scale)
        total += price_h
    return total


def _composite_card_content_fits(
    draw: ImageDraw.ImageDraw,
    *,
    title: str | None,
    price: str | None,
    inner_width: int,
    inner_height: int,
    scale: float,
) -> bool:
    text_h = _composite_card_text_height(
        draw,
        title=title,
        price=price,
        inner_width=inner_width,
        scale=scale,
    )
    gaps = 0
    if text_h:
        gaps += _scaled(_COMPOSITE_IMG_TEXT_GAP, scale)
    min_image_h = max(1, int(inner_height * _COMPOSITE_MIN_IMAGE_RATIO))
    return text_h + gaps + min_image_h <= inner_height


def _composite_content_scale(
    draw: ImageDraw.ImageDraw,
    products: list[CreatorsProductCard],
    slots: list[CompositeCardSlot],
) -> float:
    for scale in (1.0, 0.92, 0.85, 0.78, 0.72, 0.66, 0.60):
        if all(
            _composite_card_content_fits(
                draw,
                title=product.title,
                price=product.price,
                inner_width=slot.width - 2 * _scaled(_COMPOSITE_CARD_PAD, scale),
                inner_height=slot.height - 2 * _scaled(_COMPOSITE_CARD_PAD, scale),
                scale=scale,
            )
            for product, slot in zip(products, slots)
        ):
            return scale
    return 0.60


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


def _draw_composite_title(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    title: str,
    panel_x: int,
    panel_width: int,
    scale: float,
) -> int:
    title_font, lines = _resolve_composite_title_layout(
        draw, title, panel_width, scale
    )
    line_gap = _scaled(_TITLE_LINE_GAP, scale)
    for line in lines:
        line_h = _draw_aligned_text(
            draw,
            panel_x,
            y,
            line,
            title_font,
            _BLACK_TEXT,
            panel_x,
            panel_width,
            _contains_arabic(line),
        )
        y += line_h + line_gap
    if lines:
        y -= line_gap
    return y


def _draw_composite_price(
    draw: ImageDraw.ImageDraw,
    *,
    y: int,
    price: str,
    panel_x: int,
    panel_width: int,
    scale: float,
) -> int:
    return draw_price_card(
        draw,
        y=y,
        price=price,
        panel_x=panel_x,
        panel_width=panel_width,
        rtl=True,
        scale=scale,
    )


def _draw_composite_product_card(
    canvas: Image.Image,
    slot: CompositeCardSlot,
    product: CreatorsProductCard,
    scale: float,
) -> None:
    draw = ImageDraw.Draw(canvas)
    pad = _scaled(_COMPOSITE_CARD_PAD, scale)
    inner_x = slot.x + pad
    inner_y = slot.y + pad
    inner_w = max(1, slot.width - pad * 2)
    inner_h = max(1, slot.height - pad * 2)

    text_h = _composite_card_text_height(
        draw,
        title=product.title,
        price=product.price,
        inner_width=inner_w,
        scale=scale,
    )
    img_text_gap = _scaled(_COMPOSITE_IMG_TEXT_GAP, scale) if text_h else 0
    image_h = max(1, inner_h - text_h - img_text_gap)
    image_area = _render_card_product_image(product.image_path, inner_w, image_h)
    canvas.paste(image_area, (inner_x, inner_y))

    cursor_y = inner_y + image_h + img_text_gap
    has_title = bool(product.title and product.title.strip() != "Not found")
    has_price = _valid_price(product.price)

    if has_title:
        cursor_y = _draw_composite_title(
            draw,
            y=cursor_y,
            title=product.title.strip(),
            panel_x=inner_x,
            panel_width=inner_w,
            scale=scale,
        )

    if has_price:
        if has_title:
            cursor_y += _scaled(_COMPOSITE_TITLE_PRICE_GAP, scale)
        _draw_composite_price(
            draw,
            y=cursor_y,
            price=product.price.strip(),
            panel_x=inner_x,
            panel_width=inner_w,
            scale=scale,
        )


def _apply_frame_creators_composite(
    output_path: str,
    products: list[CreatorsProductCard],
) -> str:
    """Render 2–6 products in an automatic composite grid inside the frame."""
    frame_path = "frame.png"
    frame = Image.open(frame_path).convert("RGBA")
    geo = get_frame_geometry(frame)

    canvas = Image.new("RGBA", (geo.slot_width, geo.slot_height), (255, 255, 255, 255))
    slots = _composite_card_slots(len(products), geo.slot_width, geo.slot_height)
    probe = ImageDraw.Draw(canvas)
    layout_scale = _composite_content_scale(probe, products, slots)

    for product, slot in zip(products, slots):
        _draw_composite_product_card(canvas, slot, product, layout_scale)

    final_canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    final_canvas.paste(canvas, (geo.slot_x, geo.slot_y))
    final = Image.alpha_composite(final_canvas, frame)
    final.save(output_path)
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
