"""Affiliate tag helper for caption/published links.

This intentionally does not modify scrape URLs; call sites should only use it
when building the final/display URL for captions/publishing.
"""

from __future__ import annotations

import logging
import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_TAG_ENABLED: bool = False
_TAG_VALUE: str = ""

_TAG_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def set_affiliate_settings(enabled: bool, value: str) -> None:
    global _TAG_ENABLED, _TAG_VALUE
    _TAG_ENABLED = bool(enabled)
    _TAG_VALUE = (value or "").strip()


def is_valid_affiliate_tag(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and bool(_TAG_RE.fullmatch(v))


def apply_affiliate_tag(url: str) -> str:
    """Append/update `tag=` query parameter if affiliate is enabled."""
    if not _TAG_ENABLED:
        return url
    tag = _TAG_VALUE
    if not tag:
        return url
    if not url:
        return url

    parts = urlsplit(url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)

    # Replace existing tag param(s)
    filtered = [(k, v) for (k, v) in query_pairs if k.lower() != "tag"]
    filtered.append(("tag", tag))
    new_query = urlencode(filtered, doseq=True)

    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )

