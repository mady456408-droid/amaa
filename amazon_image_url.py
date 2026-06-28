"""Amazon product image URL helpers — highest available resolution without PIL."""

from __future__ import annotations

import re
from typing import Any

# Highest-first CDN dimension candidates (graceful fallback on download).
_RESOLUTION_TARGETS = (3000, 2000, 1500)

_SL_PATTERN = re.compile(r"_SL(\d+)_")
_UX_PATTERN = re.compile(r"_UX(\d+)_")
_UY_PATTERN = re.compile(r"_UY(\d+)_")
_SR_PATTERN = re.compile(r"_SR(\d+),(\d+)_")


def _url_dimension_hint(url: str) -> int:
    """Best-effort max dimension from Amazon image URL suffixes."""
    hints: list[int] = []
    m = _SL_PATTERN.search(url)
    if m:
        hints.append(int(m.group(1)))
    m = _UX_PATTERN.search(url)
    if m:
        hints.append(int(m.group(1)))
    m = _UY_PATTERN.search(url)
    if m:
        hints.append(int(m.group(1)))
    m = _SR_PATTERN.search(url)
    if m:
        hints.append(max(int(m.group(1)), int(m.group(2))))
    return max(hints) if hints else 0


def _apply_dimension(url: str, dimension: int) -> str:
    """Rewrite known Amazon CDN size suffixes to a target dimension."""
    if not url:
        return url

    def _set_sl(match: re.Match[str]) -> str:
        return f"_SL{dimension}_"

    def _set_ux(match: re.Match[str]) -> str:
        return f"_UX{dimension}_"

    def _set_uy(match: re.Match[str]) -> str:
        return f"_UY{dimension}_"

    def _set_sr(match: re.Match[str]) -> str:
        return f"_SR{dimension},{dimension}_"

    upgraded = url
    if _SL_PATTERN.search(upgraded):
        upgraded = _SL_PATTERN.sub(_set_sl, upgraded)
    if _UX_PATTERN.search(upgraded):
        upgraded = _UX_PATTERN.sub(_set_ux, upgraded)
    if _UY_PATTERN.search(upgraded):
        upgraded = _UY_PATTERN.sub(_set_uy, upgraded)
    if _SR_PATTERN.search(upgraded):
        upgraded = _SR_PATTERN.sub(_set_sr, upgraded)
    return upgraded


def amazon_image_url_candidates(url: str | None) -> list[str]:
    """
    Return Amazon CDN URLs from highest to lowest resolution.
    Supports _SL_, _AC_SL_, _AC_UY_, _UX_, _UY_, and _SR_ patterns.
    """
    if not url:
        return []

    candidates: list[str] = []
    seen: set[str] = set()

    has_size_token = bool(
        _SL_PATTERN.search(url)
        or _UX_PATTERN.search(url)
        or _UY_PATTERN.search(url)
        or _SR_PATTERN.search(url)
    )

    if has_size_token:
        for dim in _RESOLUTION_TARGETS:
            candidate = _apply_dimension(url, dim)
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)

    if url not in seen:
        candidates.append(url)

    return candidates


def upgrade_amazon_image_url(url: str | None) -> str | None:
    """Return the highest-resolution candidate URL (first in fallback list)."""
    candidates = amazon_image_url_candidates(url)
    return candidates[0] if candidates else url


def pick_best_primary_image_url(primary: Any) -> str | None:
    """Pick the largest primary image URL from Creators API primary image object."""
    if not isinstance(primary, dict):
        return None

    candidates: list[tuple[int, str]] = []
    for size_key in ("large", "medium", "small"):
        entry = primary.get(size_key)
        if not isinstance(entry, dict):
            continue
        raw_url = (entry.get("url") or "").strip()
        if not raw_url:
            continue
        url = upgrade_amazon_image_url(raw_url) or raw_url
        hint = _url_dimension_hint(url)
        height = entry.get("height")
        width = entry.get("width")
        if isinstance(height, int):
            hint = max(hint, height)
        if isinstance(width, int):
            hint = max(hint, width)
        candidates.append((hint, url))

    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]
