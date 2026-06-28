from __future__ import annotations

import os
from typing import NamedTuple

import arabic_reshaper
from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont

from coupon_price import parse_price_number
from PIL import features

_HAS_RAQM = features.check("raqm")

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
_TITLE_FONT_MAX = 64
_TITLE_FONT_MIN = 38
_TITLE_LINE_GAP = 16
_OLD_PRICE_FONT = 34
_OLD_PRICE_STRIKE_WIDTH = 2
_PRICE_LABEL_FONT = 28
_PRICE_CURRENCY_FONT = 38
_PRICE_NUM_MAX = 99
_PRICE_NUM_MIN = 64
_PRICE_CARD_PAD_X = 50
_PRICE_CARD_PAD_Y = 35
_PRICE_CARD_RADIUS = 26
_PRICE_CARD_INNER_RESERVE = 100
_PRICE_CARD_WIDTH_BOOST = 1.12
_PRICE_NUM_CURRENCY_GAP = 11
_DISCOUNT_BADGE_FONT = 40
_DISCOUNT_BADGE_PAD_X = 28
_DISCOUNT_BADGE_PAD_Y = 16
_DISCOUNT_BADGE_RADIUS = 19
_AMAZON_YELLOW = (255, 216, 20, 255)
_GRAY_TEXT = (120, 120, 120, 255)
_LABEL_GRAY = (85, 85, 85, 255)
_BLACK_TEXT = (20, 20, 20, 255)
_DISCOUNT_RED = (190, 35, 35, 255)
_PRIME_BLUE_LIGHT = (0, 168, 225, 105)
_PRIME_BADGE_FONT = 23
_PRIME_BADGE_PAD_X = 17
_PRIME_BADGE_PAD_Y = 7
_PRIME_BADGE_RADIUS = 13
_WHITE_THRESHOLD = 248
_TRANSPARENT_ALPHA = 12
_CORNER_BADGE_MARGIN = 32
_PRICE_LABEL = "السعر الآن"
_OLD_PRICE_LABEL = "بدلاً من"
_CURRENCY_LABEL = "جنيه"


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


def _try_font_path(path: str, size: int) -> ImageFont.FreeTypeFont | None:
    if not os.path.isfile(path):
        return None
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return None


_ARABIC_FONT_PROBE_CODES = (0x0627, 0x0628, 0x062A)
_ARABIC_TITLE_FONT_BLOCKED = frozenset(
    name.lower()
    for name in (
        "seguibl.ttf",
        "seguibl.ttc",
        "ariblk.ttf",
        "arialblk.ttf",
        "arial black.ttf",
        "notosans-black.ttf",
        "notosans-extrabold.ttf",
    )
)


def _font_path_blocked_for_arabic(path: str) -> bool:
    return os.path.basename(path).lower() in _ARABIC_TITLE_FONT_BLOCKED


def _font_supports_arabic(font: ImageFont.ImageFont) -> bool:
    if not isinstance(font, ImageFont.FreeTypeFont):
        return False

    path = getattr(font, "path", "") or ""
    if path and _font_path_blocked_for_arabic(path):
        return False

    try:
        if hasattr(font, "has_glyph"):
            if not all(font.has_glyph(code) for code in _ARABIC_FONT_PROBE_CODES):
                return False
    except OSError:
        return False

    try:
        ft_font = font.font
        get_char_index = getattr(ft_font, "get_char_index", None)
        if callable(get_char_index):
            if any(get_char_index(code) == 0 for code in _ARABIC_FONT_PROBE_CODES):
                return False
    except Exception:
        return False

    try:
        bbox = font.getbbox("\u0627\u0628\u062a")
        if not bbox or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            return False
    except OSError:
        return False

    return True


def _first_usable_font(
    paths: list[str],
    size: int,
    *,
    require_arabic: bool,
) -> ImageFont.FreeTypeFont | None:
    seen: set[str] = set()
    for path in paths:
        normalized = os.path.normcase(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        if require_arabic and _font_path_blocked_for_arabic(path):
            continue
        font = _try_font_path(path, size)
        if font is None:
            continue
        if require_arabic and not _font_supports_arabic(font):
            continue
        return font
    return None


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
    "/usr/share/fonts/truetype/noto/NotoSansArabic-SemiBold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-SemiBold.ttf",
    "C:/Windows/Fonts/NotoSansArabic-SemiBold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
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
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
    "C:/Windows/Fonts/NotoSansArabic-Bold.ttf",
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_title_font(
    size: int,
    *,
    title: str | None = None,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    needs_arabic = bool(title and _contains_arabic(title))

    if needs_arabic:
        font = _first_usable_font(
            _ARABIC_TITLE_FONT_CANDIDATES,
            size,
            require_arabic=True,
        )
        if font is not None:
            return font
        font = _first_usable_font(
            _ARABIC_TITLE_FONT_FALLBACKS,
            size,
            require_arabic=True,
        )
        if font is not None:
            return font
        return _load_font(size, bold=True)

    font = _first_usable_font(
        _LATIN_TITLE_FONT_CANDIDATES,
        size,
        require_arabic=False,
    )
    if font is not None:
        return font
    return _load_font(size, bold=True)


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


def _load_badge_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return _load_font(size, bold=False)


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
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if _text_bbox(draw, trial, font)[0] <= max_width:
            current = trial
            continue
        if current:
            lines.append(current)
            if len(lines) >= max_lines:
                break
        current = word
        if _text_bbox(draw, current, font)[0] > max_width:
            current = _truncate_line(draw, word, font, max_width)

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
        font = _load_font(_scaled(_OLD_PRICE_FONT, scale), bold=False)
        number, currency = _parse_price_display(list_price.strip())
        text = f"{_OLD_PRICE_LABEL} {number} {currency}"
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
    max_size = _scaled(_TITLE_FONT_MAX, scale)
    min_size = max(20, _scaled(_TITLE_FONT_MIN, scale))
    step = max(2, _scaled(2, scale))
    for size in range(max_size, min_size - 1, -step):
        font = _load_title_font(size, title=title)
        lines = _wrap_title_lines(draw, title, font, max_width, _TITLE_MAX_LINES)
        if lines:
            return font, lines
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
        font = _load_font(size, bold=True)
        if _text_bbox(draw, number_text, font)[0] <= inner_max:
            return font, size
    font = _load_font(min_size, bold=True)
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
    label_font = _load_font(_scaled(_PRICE_LABEL_FONT, scale), bold=False)
    curr_font = _load_font(_scaled(_PRICE_CURRENCY_FONT, scale), bold=False)

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
    font = _load_font(_scaled(_OLD_PRICE_FONT, scale), bold=False)
    number, currency = _parse_price_display(list_price)
    text = f"{_OLD_PRICE_LABEL} {number} {currency}"
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
    title_font, lines = _resolve_title_layout(draw, title, panel_width, scale)
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
    if trailing_gap:
        return y + _scaled(_TITLE_AFTER_GAP, scale)
    return y


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
    font = _load_font(_scaled(_OLD_PRICE_FONT, scale), bold=False)
    number, currency = _parse_price_display(list_price)
    text = f"{_OLD_PRICE_LABEL} {number} {currency}"
    tw, th = _text_bbox(draw, text, font)
    draw_x = panel_x + panel_width - tw if rtl else panel_x
    _draw_text(draw, (draw_x, y), text, font, _GRAY_TEXT)
    strike_y = y + th // 2
    draw.line(
        (draw_x, strike_y, draw_x + tw, strike_y),
        fill=_GRAY_TEXT,
        width=max(1, _scaled(_OLD_PRICE_STRIKE_WIDTH, scale)),
    )
    return y + th


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
    font = _load_badge_font(_scaled(_DISCOUNT_BADGE_FONT, scale))
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
    font = _load_badge_font(_scaled(_PRIME_BADGE_FONT, scale))
    text = "prime"
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
