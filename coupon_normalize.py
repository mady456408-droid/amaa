"""Normalize Amazon clip-coupon widget text for Arabic captions."""

import logging
import re

logger = logging.getLogger(__name__)

_INVISIBLE = ("\u200f", "\u200e", "\u202a", "\u202b", "\u202c", "\ufeff", "\xa0")

_MAX_RAW_LEN = 200
_MAX_OUTPUT_LEN = 80

_GARBAGE_PHRASES = (
    "الصورة غير متوفرة",
    "image not available",
    "currently unavailable",
    "add to cart",
    "buy now",
    "see all",
    "عرض الكل",
    "تفاصيل المنتج",
)

# English clip-coupon patterns
_RE_APPLY_EGP_COUPON = re.compile(
    r"(?i)apply\s+egp\s*([\d,]+(?:\.\d+)?)\s*coupon"
)
_RE_EGP_OFF_COUPON_APPLIED = re.compile(
    r"(?i)egp\s*([\d,]+(?:\.\d+)?)\s*off\s+coupon\s*applied?"
)
_RE_APPLY_PCT_COUPON = re.compile(
    r"(?i)apply\s*([\d,]+(?:\.\d+)?)\s*%\s*coupon"
)
_RE_PCT_COUPON = re.compile(r"(?i)([\d,]+(?:\.\d+)?)\s*%\s*coupon")

# Arabic Amazon coupon patterns
_RE_AR_TATBIQ_EGP_COUPON = re.compile(
    r"(?i)تطبيق\s*egp\s*([\d,]+(?:\.\d+)?)\s*كوبون"
)
_RE_AR_COUPON_EGP = re.compile(
    r"(?i)كوبون\s*egp\s*([\d,]+(?:\.\d+)?)"
)
_RE_AR_TATBIQ_COUPON_PCT = re.compile(
    r"تطبيق\s*كوبون\s*([\d,]+(?:\.\d+)?)\s*%"
)
_RE_AR_WAFR_JINEH = re.compile(r"وفر\s*([\d,]+(?:\.\d+)?)\s*جنيه")
_RE_AR_COUPON_JINEH = re.compile(r"كوبون\s*([\d,]+(?:\.\d+)?)\s*جنيه")
_RE_AR_KHASM_PCT = re.compile(r"خصم\s*([\d,]+(?:\.\d+)?)\s*%")


def _strip_invisible(text: str) -> str:
    out = text
    for ch in _INVISIBLE:
        out = out.replace(ch, "")
    return re.sub(r"\s+", " ", out).strip()


def _preclean_coupon_raw(text: str) -> str:
    """Remove CSS garbage and Amazon UI noise before pattern matching."""
    t = _strip_invisible(text)

    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = re.sub(
        r"(?i)\b(?:padding-(?:left|right|top|bottom)|margin|display|font-size|line-height)\b[^;\s]*",
        " ",
        t,
    )
    t = re.sub(r"(?i)\bdata-selector\b[^;\s]*", " ", t)

    t = re.split(
        r"(?:الشروط|رجوع|إغلاق|terms|conditions|see details|learn more|عرض التفاصيل)",
        t,
        maxsplit=1,
        flags=re.I,
    )[0]

    return _strip_invisible(t)


def _is_label_only(t: str) -> bool:
    return bool(re.fullmatch(r"(?i)coupons?\s*:?", t.strip())) or t.strip() in (
        "كوبون",
        "كوبون:",
    )


def _has_coupon_keyword(t: str) -> bool:
    return bool(re.search(r"(?i)\bcoupons?\b", t)) or "كوبون" in t


def _matches_core_patterns(t: str) -> bool:
    return bool(
        _RE_APPLY_EGP_COUPON.search(t)
        or _RE_EGP_OFF_COUPON_APPLIED.search(t)
        or _RE_APPLY_PCT_COUPON.search(t)
        or _RE_PCT_COUPON.search(t)
        or _RE_AR_TATBIQ_EGP_COUPON.search(t)
        or _RE_AR_COUPON_EGP.search(t)
        or _RE_AR_TATBIQ_COUPON_PCT.search(t)
        or _RE_AR_WAFR_JINEH.search(t)
        or _RE_AR_COUPON_JINEH.search(t)
        or _RE_AR_KHASM_PCT.search(t)
    )


def matches_coupon_pattern(text: str | None) -> bool:
    """True for Amazon clip-coupon widget text (English or Arabic)."""
    if not text:
        return False

    t = _preclean_coupon_raw(text)
    if len(t) < 4 or len(t) > _MAX_RAW_LEN:
        return False

    if _is_label_only(t):
        return False

    lower = t.lower()
    for phrase in _GARBAGE_PHRASES:
        if phrase in lower or phrase in t:
            return False

    if _matches_core_patterns(t):
        return True

    if not re.search(r"\d", t):
        return False

    if _has_coupon_keyword(t):
        if re.search(r"(?i)egp\s*[\d,]+", t):
            return True
        if _RE_PCT_COUPON.search(t):
            return True
        if re.search(r"(?i)apply\s+.*coupon", t):
            return True
        if re.search(r"تطبيق\s+.*كوبون", t):
            return True

    if re.search(r"(وفر|كوبون|خصم)", t) and re.search(r"\d", t):
        if re.search(r"جنيه|%", t) or re.search(r"(?i)egp", t):
            return True

    return False


def _pick_coupon_line(raw: str) -> str | None:
    """Best single line or merged widget text for normalization."""
    cleaned_raw = _preclean_coupon_raw(raw)
    lines = [
        _preclean_coupon_raw(line)
        for line in re.split(r"[\n\r]+", raw)
        if _strip_invisible(line)
    ]
    combined = _preclean_coupon_raw(" ".join(lines))

    candidates: list[str] = []
    for line in lines + [combined, cleaned_raw]:
        if line and matches_coupon_pattern(line):
            candidates.append(line)

    if not candidates:
        return None

    def score(s: str) -> int:
        sl = s.lower()
        if _RE_AR_TATBIQ_EGP_COUPON.search(s):
            return 110
        if "apply" in sl and "egp" in sl and "coupon" in sl:
            return 100
        if "تطبيق" in s and "كوبون" in s and re.search(r"(?i)egp", s):
            return 105
        if "apply" in sl and "%" in s and "coupon" in sl:
            return 95
        if "off coupon applied" in sl:
            return 90
        if _RE_PCT_COUPON.search(s) or _RE_AR_KHASM_PCT.search(s):
            return 85
        if "coupon" in sl or "كوبون" in s:
            return 50
        return 10

    return max(candidates, key=score)


def _normalize_from_text(text: str) -> str | None:
    """Normalize a single validated coupon string."""
    text = _preclean_coupon_raw(text)

    # Arabic: تطبيق EGP60 كوبون
    m = _RE_AR_TATBIQ_EGP_COUPON.search(text)
    if m:
        amount = m.group(1).replace(",", "")
        return f"كوبون إضافي {amount} جنيه"

    # English: Apply EGP60 coupon
    m = _RE_APPLY_EGP_COUPON.search(text)
    if m:
        amount = m.group(1).replace(",", "")
        return f"كوبون إضافي {amount} جنيه"

    # Arabic: كوبون EGP60
    m = _RE_AR_COUPON_EGP.search(text)
    if m:
        amount = m.group(1).replace(",", "")
        return f"كوبون إضافي {amount} جنيه"

    m = _RE_EGP_OFF_COUPON_APPLIED.search(text)
    if m:
        amount = m.group(1).replace(",", "")
        return f"كوبون خصم {amount} جنيه"

    m = _RE_APPLY_PCT_COUPON.search(text) or _RE_PCT_COUPON.search(text)
    if m:
        pct = m.group(1).replace(",", "")
        return f"كوبون خصم {pct}%"

    m = _RE_AR_TATBIQ_COUPON_PCT.search(text)
    if m:
        pct = m.group(1).replace(",", "")
        return f"كوبون خصم {pct}%"

    m = _RE_AR_WAFR_JINEH.search(text) or _RE_AR_COUPON_JINEH.search(text)
    if m:
        amount = m.group(1).replace(",", "")
        return f"كوبون إضافي {amount} جنيه"

    m = _RE_AR_KHASM_PCT.search(text)
    if m:
        pct = m.group(1).replace(",", "")
        return f"كوبون خصم {pct}%"

    if re.search(r"(?i)apply\s+.*coupon", text) or re.search(
        r"تطبيق\s+.*كوبون", text
    ):
        m = re.search(r"(?i)egp\s*([\d,]+(?:\.\d+)?)", text)
        if m:
            return f"كوبون إضافي {m.group(1).replace(',', '')} جنيه"
        m = re.search(r"([\d,]+(?:\.\d+)?)\s*%", text)
        if m:
            return f"كوبون خصم {m.group(1).replace(',', '')}%"

    return None


def normalize_coupon_text(raw: str | None) -> str | None:
    """
    Normalize clip-coupon widget text to Arabic caption fragment (no 🎟 prefix).
    """
    if not raw:
        return None

    combined = _preclean_coupon_raw(raw.replace("\n", " "))
    picked = _pick_coupon_line(raw)

    for candidate in (picked, combined, _preclean_coupon_raw(raw)):
        if not candidate:
            continue
        if not matches_coupon_pattern(candidate):
            continue
        normalized = _normalize_from_text(candidate)
        if normalized and len(normalized) <= _MAX_OUTPUT_LEN:
            logger.info("COUPON NORMALIZED: %s", normalized)
            return normalized

    logger.info("COUPON REJECTED (invalid pattern)")
    return None


def coupon_caption_line(coupon: str | None) -> str | None:
    if not coupon:
        return None
    return f"🎟 {coupon}"
