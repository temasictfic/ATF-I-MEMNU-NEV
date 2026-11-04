"""Text cleaning utilities for source processing."""

import re
import unicodedata

# Matches exactly the source-number prefixes used by source detection.
REF_NUMBER_PREFIX_PATTERNS = [
    re.compile(r"^\s*\[(\d{1,3})\]\s*"),   # [1] Text...
    re.compile(r"^\s*(\d{1,3})\.\s+"),     # 1. Text...
    re.compile(r"^\s*(\d{1,3})\)\s+"),     # 1) Text...
    re.compile(r"^\s*(\d{1,3})-\s*"),      # 1- Text...
]

# Access-date footers/noise frequently seen in sources.
ACCESS_DATE_PATTERNS = [
    r"(?i)[,;]?\s*son\s+eri[şs]im\s+tarihi\s*:?\s*\d{1,2}\s+[A-Za-zÇĞİÖŞÜçğıöşü]+\s+\d{4}\.?",
    r"(?i)[,;]?\s*eri[şs]im\s+tarihi\s*:?\s*\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\.?",
]

# Four-digit publication years 1900–2099.
YEAR_PATTERN = re.compile(r"\b((?:19|20)\d{2})\b")

# Author conjunctions ("&", "and", "ve") substituted by ", " in author lists.
# Order is significant: ampersand first, then English "and" (case-insensitive),
# then Turkish "ve". Keep this contract — call sites that previously inlined
# these substitutions relied on this ordering.
_CONJUNCTION_AMP = re.compile(r"\s+&\s+")
_CONJUNCTION_AND = re.compile(r"\s+and\s+", re.IGNORECASE)
_CONJUNCTION_VE = re.compile(r"\s+ve\s+")


def is_valid_year(val: int) -> bool:
    """True if `val` is a plausible publication year (1900–2099)."""
    return 1900 <= val <= 2099


def normalize_author_conjunctions(text: str) -> str:
    """Replace " & ", " and ", " ve " separators with ", " in an author list."""
    text = _CONJUNCTION_AMP.sub(", ", text)
    text = _CONJUNCTION_AND.sub(", ", text)
    text = _CONJUNCTION_VE.sub(", ", text)
    return text


def normalize_text(text: str) -> str:
    """Normalize Unicode text for comparison."""
    text = unicodedata.normalize("NFKD", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_source_noise(text: str) -> str:
    """Remove leading source numbering and access-date fragments."""
    cleaned = text or ""

    for pattern in REF_NUMBER_PREFIX_PATTERNS:
        next_cleaned = pattern.sub("", cleaned, count=1)
        if next_cleaned != cleaned:
            cleaned = next_cleaned
            break

    for pattern in ACCESS_DATE_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned)

    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_source_text(text: str) -> str:
    """Clean a source text for search queries."""
    text = strip_source_noise(text)
    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)
    # Remove DOIs
    text = re.sub(r"doi[:\s]*10\.\S+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"10\.\d{4,9}/\S+", "", text)
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
