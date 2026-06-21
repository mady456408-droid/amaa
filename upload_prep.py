"""Telegram upload prep only — does not alter screenshot or framing."""

import os

from PIL import Image

from config import JPEG_QUALITY, UPLOAD_MAX_BYTES

_RESAMPLE = Image.Resampling.LANCZOS


def to_jpeg_for_telegram(image_path: str) -> str:
    """Convert framed PNG to high-quality JPEG for channel publish."""
    if image_path.lower().endswith((".jpg", ".jpeg")):
        return image_path

    out_path = f"{os.path.splitext(image_path)[0]}_upload.jpg"
    img = Image.open(image_path)

    # If image has alpha channel, composite onto white background
    # to prevent transparent pixels from becoming black in JPEG
    if img.mode == "RGBA":
        white_bg = Image.new("RGB", img.size, (255, 255, 255))
        white_bg.paste(img, mask=img.split()[3])  # Use alpha channel as mask
        img = white_bg
    else:
        img = img.convert("RGB")

    img.save(
        out_path,
        "JPEG",
        quality=JPEG_QUALITY,
        subsampling=0,
        optimize=False,
    )

    size = os.path.getsize(out_path)
    if size <= UPLOAD_MAX_BYTES:
        return out_path

    scale = 0.9
    while size > UPLOAD_MAX_BYTES and scale >= 0.4:
        w, h = img.size
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            _RESAMPLE,
        )
        img.save(
            out_path,
            "JPEG",
            quality=JPEG_QUALITY,
            subsampling=0,
            optimize=False,
        )
        size = os.path.getsize(out_path)
        scale *= 0.9

    return out_path


def prepare_channel_upload(image_path: str) -> tuple[str, bool]:
    """
    Return path suitable for destination channel publish.
    Second value is True when a temporary JPEG was created from PNG.
    """
    jpeg_path = to_jpeg_for_telegram(image_path)
    return jpeg_path, jpeg_path != image_path
