"""Detect citation format (APA, MLA, Chicago, Harvard, Vancouver, IEEE) from source text.

Based on Kurallar.xlsx Sheet 2 "Atif Formatlari" distinguishing characteristics.
"""

import re
from enum import Enum


class CitationFormat(str, Enum):
    APA = "APA"
    MLA = "MLA"
    CHICAGO = "Chicago"
    HARVARD = "Harvard"
    VANCOUVER = "Vancouver"
    IEEE = "IEEE"


def detect_format(text: str) -> tuple[CitationFormat | None, float]:
    """Detect citation format from source text.

    Returns (format, confidence) where confidence is 0.0-1.0.
    Returns (None, 0.0) if no format can be determined.
    """
    if not text or len(text) < 20:
        return None, 0.0

    scores: dict[CitationFormat, float] = {fmt: 0.0 for fmt in CitationFormat}

    # --- Signal 1: Year in parentheses after period (APA/MLA/Chicago) ---
    if re.search(r"\.\s*\(\d{4}", text):
        scores[CitationFormat.APA] += 2
        scores[CitationFormat.MLA] += 1
        scores[CitationFormat.CHICAGO] += 1

    # --- Signal 2: Year without parentheses after period (Harvard/Vancouver) ---
    if re.search(r"[.,]\s*\d{4}\.\s+[A-Z]", text):
        scores[CitationFormat.HARVARD] += 2
        scores[CitationFormat.VANCOUVER] += 2

    # --- Signal 3: Year near end of source (IEEE) ---
    # Only boost IEEE if year is near end AND not in parentheses (MLA/Chicago have (Year) near end too)
    year_matches = list(re.finditer(r"\b(?:19|20)\d{2}\b", text))
    if year_matches:
        last_year_pos = year_matches[-1].start()
        last_year_in_parens = bool(re.search(r"\(" + text[last_year_pos:last_year_pos + 4] + r"\)", text))
        if len(text) > 0 and last_year_pos / len(text) > 0.7:
            if not last_year_in_parens:
                scores[CitationFormat.IEEE] += 3
            else:
                # Year in parens near end = MLA or Chicago
                scores[CitationFormat.MLA] += 2
                scores[CitationFormat.CHICAGO] += 2

    # --- Signal 4: Title in double quotes (MLA/Chicago/IEEE) ---
    has_quotes = bool(re.search(r'["\u201C][^"\u201D]{10,}["\u201D]', text))
    if has_quotes:
        scores[CitationFormat.MLA] += 2
        scores[CitationFormat.CHICAGO] += 2
        scores[CitationFormat.IEEE] += 2

    # --- Signal 5: Title NOT in quotes (APA/Harvard/Vancouver) ---
    if not has_quotes:
        scores[CitationFormat.APA] += 1
        scores[CitationFormat.HARVARD] += 1
        scores[CitationFormat.VANCOUVER] += 1

    # --- Signal 6: ", K." abbreviated initials with period (APA/Harvard) ---
    if re.search(r",\s*[A-Z]\.", text[:150]):
        scores[CitationFormat.APA] += 1
        scores[CitationFormat.HARVARD] += 1

    # --- Signal 7: No comma between last name and initial (Vancouver) ---
    # Vancouver: "Shingjergji K, Iren D" — LastName Initial(s) without comma/period between
    if re.search(r"^[A-Z][a-z]+\s[A-Z]{1,3}[,.]", text):
        scores[CitationFormat.VANCOUVER] += 3

    # --- Signal 8: No period after initial (Vancouver) ---
    # Vancouver initials have no period: "Liu G," not "Liu G.,"
    if re.search(r"\s[A-Z]{1,3},\s", text[:150]) and not re.search(r"\s[A-Z]\.\s*,", text[:150]):
        scores[CitationFormat.VANCOUVER] += 2

    # --- Signal 9: Initials BEFORE last name (IEEE) ---
    # IEEE: "G. Liu" or "K. Y. Lee" at start or after comma
    # Must NOT match "Author, K." which is APA-style (initial AFTER last name)
    if re.match(r"^[A-Z]\.\s*[A-Z]", text) or re.search(r",\s+[A-Z]\.\s*[A-Z]\.?\s+[A-Z][a-z]", text[:150]):
        scores[CitationFormat.IEEE] += 3
    # Negative signal: full author names (not initials-first) = NOT IEEE
    if re.match(r"^[A-Z][a-z]{2,},\s+[A-Z][a-z]{2,}", text):
        scores[CitationFormat.IEEE] -= 2

    # --- Signal 10: Title ends with ," (IEEE) ---
    if re.search(r'["\u201D],\s', text) or re.search(r',\s*["\u201D]\s', text):
        scores[CitationFormat.IEEE] += 2

    # --- Signal 11: "et al." usage (APA/MLA) ---
    if re.search(r"et\s+al\.", text, re.IGNORECASE):
        scores[CitationFormat.APA] += 1
        scores[CitationFormat.MLA] += 1

    # --- Signal 12: "and" connector listing all authors (Chicago) ---
    # Chicago lists all authors with "and", no "et al."
    if re.search(r"\band\b", text[:200]) and not re.search(r"et\s+al\.", text, re.IGNORECASE):
        scores[CitationFormat.CHICAGO] += 2
        # If full names + "and" + quotes: strong Chicago signal
        if has_quotes and re.match(r"^[A-Z][a-z]{2,},\s+[A-Z][a-z]{2,}", text):
            scores[CitationFormat.CHICAGO] += 1

    # --- Signal 13: "&" connector (APA/Harvard) ---
    if re.search(r"\s&\s", text[:200]):
        scores[CitationFormat.APA] += 1
        scores[CitationFormat.HARVARD] += 1

    # --- Signal 14: Year immediately after authors (position 2) ---
    # APA: "Author, K. (2025)." / Harvard: "Author, K., 2025."
    if re.search(r"^[A-Z].*?[.,]\s*\(?\d{4}\)?[.,]", text[:200]):
        # Year appears early — check if it's position 2 (right after authors)
        first_year = re.search(r"\b(?:19|20)\d{2}\b", text)
        if first_year and first_year.start() < len(text) * 0.35:
            scores[CitationFormat.APA] += 1
            scores[CitationFormat.HARVARD] += 1

    # --- Signal 15: "vol." or "pp." patterns (IEEE) ---
    if re.search(r"\bvol\.\s*\d", text, re.IGNORECASE):
        scores[CitationFormat.IEEE] += 1

    # Find the best and second-best scores
    sorted_formats = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_fmt, best_score = sorted_formats[0]
    second_score = sorted_formats[1][1] if len(sorted_formats) > 1 else 0

    # Minimum threshold
    if best_score < 3:
        return None, 0.0

    # Confidence: higher when the gap between best and second is large
    max_possible = 12.0  # approximate max achievable score
    base_confidence = min(best_score / max_possible, 1.0)

    # Reduce confidence if top two are close
    gap = best_score - second_score
    if gap <= 1:
        base_confidence *= 0.5
    elif gap <= 2:
        base_confidence *= 0.75

    return best_fmt, round(base_confidence, 2)
