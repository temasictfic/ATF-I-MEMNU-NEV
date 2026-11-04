"""Extract structured fields (title, authors, year, DOI, etc.) from raw source text.

Implements rule-based parsing guided by Kurallar.xlsx:
- Sheet 1 "Veri": 12 parsing rules for field boundary detection
- Sheet 2 "Atif Formatlari": 6 citation format definitions (APA, MLA, Chicago, Harvard, Vancouver, IEEE)
"""

import re

from models.source import ParsedSource
from utils.doi_extractor import extract_arxiv_id, extract_doi
from utils.text_cleaning import (
    YEAR_PATTERN,
    is_valid_year,
    normalize_author_conjunctions,
    strip_source_noise,
)
from utils.url_cleaner import find_best_url

from services.citation_format_detector import CitationFormat, detect_format
from services.scoring_constants import LOW_PARSE_CONFIDENCE_THRESHOLD

# Leading inline parenthetical citation that some refs duplicate before the
# real author list, e.g. "(Jenkins vd., 2024) Jenkins, A., ve diğerleri (2024)".
# Used both on the whole text (before parsing) and on the extracted author
# section (before pairing) to defend against bracketed reprints either side
# of the boundary.
_LEADING_INLINE_CITATION_RE = re.compile(
    r"^\(\s*[^)]*?(?:vd|et\s+al|diğerleri)\.?\s*(?:,?\s*(?:19|20)\d{2})?\s*\)\s*",
    flags=re.IGNORECASE,
)


async def extract_source_fields(raw_text: str) -> ParsedSource:
    """Parse raw source text into structured fields.

    Tries NER extraction first, falls back to regex if NER is
    unavailable or returns low confidence.
    """
    from services.ner_extractor import extract_fields_ner

    # Strip leading source numbering ("1-", "1.", "[1]", "1)") before
    # NER sees the text — otherwise the numeric prefix leaks into author
    # entity spans via raw_text offset reconstruction.
    cleaned_text = strip_source_noise(raw_text)

    ner_result = await extract_fields_ner(cleaned_text)
    if ner_result is not None and ner_result.parse_confidence >= LOW_PARSE_CONFIDENCE_THRESHOLD:
        return ner_result

    return _extract_source_fields_regex(cleaned_text)


def _extract_source_fields_regex(raw_text: str) -> ParsedSource:
    """Parse raw source text into structured fields using rule-based, format-aware extraction."""
    result = ParsedSource(raw_text=raw_text)

    # Strip leading numbering and access-date noise before parsing fields.
    text = strip_source_noise(raw_text)

    # Strip a leading inline parenthetical citation that some refs
    # duplicate before the real author list, e.g.
    #   "(Jenkins vd., 2024) Jenkins, A., ve diğerleri (2024). …"
    # The parenthetical confuses every downstream rule (boundary, year,
    # author pairing), so we remove it up front.
    text = _LEADING_INLINE_CITATION_RE.sub("", text)

    # Extract identifiers for URL building
    doi = extract_doi(text)
    arxiv_id = extract_arxiv_id(text)

    # Build URL: doi > arxiv > first extracted URL
    if doi:
        result.url = f"https://doi.org/{doi}"
    elif arxiv_id:
        result.url = f"https://arxiv.org/abs/{arxiv_id}"
    else:
        result.url = find_best_url(text)

    # Detect citation format
    fmt, _fmt_confidence = detect_format(text)
    result.citation_format = fmt.value if fmt else None

    # Find author boundary using Kurallar rules
    author_end = _find_author_boundary(text, fmt)

    # Extract authors from the author section
    author_text = text[:author_end].strip().rstrip(",").rstrip(".")
    # Trailing year can leak into the author section when the boundary
    # detector stops at a title quote (e.g. IEEE book refs). Strip both
    # bare trailing years and parenthesized trailing years.
    author_text = re.sub(
        r"[\s.,]*\(?\s*(?:19|20)\d{2}[a-z]?\s*\)?\.?$", "", author_text
    ).strip().rstrip(",").rstrip(".")
    # Strip leading inline citations that some refs duplicate before the
    # real author list, e.g. "(Calazans vd. 2024) Calazans, M. A. A., ..."
    author_text = _LEADING_INLINE_CITATION_RE.sub("", author_text).strip()
    # Strip quoted nicknames embedded in the author list, e.g.
    # `Um, E. "Rachel", Plass, J. L., ...`. The nickname confuses the
    # comma-split pairing; the "Rachel" span adds no signal for matching.
    author_text = re.sub(r'[\"\u201C][^\"\u201D]*[\"\u201D]', "", author_text)
    author_text = re.sub(r"\s+", " ", author_text).strip(" ,.")
    result.authors = _parse_authors(author_text, fmt)

    # Extract year (rule-based priority: parenthesized > post-conjunction > first-after-authors)
    result.year, year_start, year_end = _extract_year(text, author_end)

    # Extract title and journal (journal/conference/publisher name)
    result.title, result.journal = _extract_title_journal(text, author_end, year_start, year_end, fmt)

    # Compute confidence
    result.parse_confidence = _compute_parse_confidence(result)

    return result


# ---------------------------------------------------------------------------
# Author boundary detection (Kurallar Rules 4, 6, 7, 8, 9, 10, 11, 12)
# ---------------------------------------------------------------------------

def _find_author_boundary(text: str, fmt: CitationFormat | None) -> int:
    """Find the character index where the authors section ends.

    Uses Kurallar author rules to detect the boundary between authors and
    the rest of the source (year, title, journal, etc.).
    """
    if not text:
        return 0

    # Format-specific overrides
    if fmt == CitationFormat.VANCOUVER:
        return _find_author_boundary_vancouver(text)
    if fmt == CitationFormat.IEEE:
        return _find_author_boundary_ieee(text)

    # Generic rule-based detection for APA/MLA/Chicago/Harvard and unknown formats

    # Rule 10 (Definite): "(YYYY)" / "(Mar. 2024)" / "(October 5, 2023)" /
    # "(2020a)" parens after authors marks the boundary. The boundary lands
    # just before the opening "(", regardless of whether the preceding char
    # is "." (APA `et al.`) or a plain space (`Ismail (Oct. 2023)`).
    for m in re.finditer(
        r"[\s.,]\("                                  # opening ( preceded by whitespace/punct
        r"(?:[A-Za-zÀ-ɏ]+\.?\s+)?"                   # optional month abbrev/full ("Oct.", "October")
        r"(?:\d{1,2}[.,]?\s+)?"                      # optional day-of-month ("5,", "22")
        r"(\d{4})[a-z]?"                             # year (with optional disambiguation letter)
        r"\s*[,)]",                                  # closing ) or comma (for "(2020, October 5)")
        text,
    ):
        year_val = int(m.group(1))
        if not is_valid_year(year_val):
            continue
        paren_pos = text.find("(", m.start(), m.end())
        if paren_pos <= 0:
            continue
        before = text[:paren_pos]
        # Reject if a quote char already appears before — that means we are
        # past the author block (e.g. Chicago `J. "Title." Journal (Year)`).
        if '"' in before or "“" in before:
            continue
        if _looks_like_author_section(before):
            return paren_pos

    # MLA/Chicago: authors end before a quoted title
    # Check if there's a ". " followed by a quote character, but only
    # if the quoted span looks like a title (several words) — not a
    # short nickname like Um, E. "Rachel", which some refs include
    # inside the author list.
    for qm in re.finditer(r'\.\s+["\u201C]', text):
        before = text[:qm.start() + 1]
        if not _looks_like_author_section(before):
            continue
        # Find the closing quote and measure word count inside.
        after_open = qm.end()
        close_m = re.search(r'["\u201D]', text[after_open:])
        if close_m:
            inside = text[after_open : after_open + close_m.start()].strip()
            if len(inside.split()) < 3:
                # Too short to be a title — likely a nickname inside authors.
                continue
        return qm.start() + 1

    # Rule 8 (Definite): "., YYYY" = last author is right before year
    for m in re.finditer(r"\.,\s*(\d{4})", text):
        year_val = int(m.group(1))
        if is_valid_year(year_val):
            return m.start() + 1  # include the period

    # Rule 9 (Definite): ". YYYY" = last author is right before year
    for m in re.finditer(r"\.\s+(\d{4})", text):
        year_val = int(m.group(1))
        if is_valid_year(year_val):
            # Make sure this isn't in the middle of a title/journal
            # Check that the text before looks like author names
            before = text[:m.start() + 1]
            if _looks_like_author_section(before):
                return m.start() + 1

    # Rule 4 (Definite): ". , " side by side = end of authors section
    # UNLESS followed by &, and, ve, et al. (still in authors)
    for m in re.finditer(r"\.\s*,\s", text):
        pos = m.end()
        after = text[pos:pos + 40].strip()
        # Rule 4 exception: if followed by conjunction, still authors
        if re.match(r"(?:&|and\b|ve\b|et\s+al\.)", after, re.IGNORECASE):
            continue
        # Rule 6/12: if followed by ", K." pattern, still in authors
        if re.match(r"[A-ZÇĞİÖŞÜ]\.", after):
            continue
        # Extension: if followed by "Surname, Initial" (another author
        # in the list), the ". ," is NOT the end of the author section.
        # e.g. "Hinton, G., Deng, L., ..." — first ". ," sits between two authors.
        if re.match(
            r"[A-ZÇĞİÖŞÜ][a-zçğıöşü]+(?:-[A-ZÇĞİÖŞÜa-zçğıöşü]+)?\s*,\s*[A-ZÇĞİÖŞÜ]",
            after,
        ):
            continue
        return m.start() + 1

    # Fallback: find the first year and use text before it
    year_match = re.search(r"\((?:19|20)\d{2}[a-z]?\)", text)
    if year_match:
        return year_match.start()

    # Fallback: find first bare year
    year_match = YEAR_PATTERN.search(text)
    if year_match:
        # Go back to the preceding period or comma
        before = text[:year_match.start()]
        last_period = before.rfind(".")
        last_comma = before.rfind(",")
        boundary = max(last_period, last_comma)
        if boundary > 0:
            return boundary + 1

    # Last resort: use first 30% of text or first period
    first_period = text.find(".")
    if first_period > 0 and first_period < len(text) * 0.5:
        return first_period + 1

    return min(len(text), int(len(text) * 0.3))


def _find_author_boundary_vancouver(text: str) -> int:
    """Find author boundary for Vancouver format.

    Vancouver: "Shingjergji K, Iren D, Urlings C, Klemke R. Title..."
    Authors have no comma between last name and initials, no period after initials.
    The FIRST period in the text marks the end of the author section.
    """
    # In Vancouver format, there are NO periods within the author section.
    # The very first period is the author/title boundary.
    # Scan for "LastName INITIALS" groups separated by commas, ending with a period.
    # Pattern: sequence of "Word CAPS" separated by commas, terminated by "."
    m = re.match(
        r"((?:[A-Z][a-z]+\s[A-Z]{1,3}(?:\s[A-Z]{1,3})*,\s*)*"  # comma-separated authors
        r"[A-Z][a-z]+\s[A-Z]{1,3}(?:\s[A-Z]{1,3})*)"            # last author (no trailing comma)
        r"\.\s",                                                    # period = boundary
        text,
    )
    if m:
        return m.end() - 1  # include up to the period, position after "."

    # Fallback: first period in text
    first_period = text.find(".")
    if first_period > 5:
        return first_period + 1
    return min(len(text), int(len(text) * 0.3))


def _find_author_boundary_ieee(text: str) -> int:
    """Find author boundary for IEEE format.

    IEEE: "G. Liu, K. Y. Lee, and H. F. Jordan, \\"Title,\\" ..."
    Authors have initials first (G. Liu), and the transition to title
    is marked by a quoted string.
    """
    # Book/editor sources often mark the transition with "Ed.," or "Eds.,"
    # and do not quote titles (e.g., "A. Author, Eds., Book Title...").
    editor_boundary = re.search(r",\s*(?:Ed\.|Eds\.|Editor|Editors),\s+", text, re.IGNORECASE)
    if editor_boundary:
        return editor_boundary.end()

    # Look for the start of a quoted title
    quote_match = re.search(r'[,\s]+["\u201C]', text)
    if quote_match:
        return quote_match.start()

    # Fallback: look for ", \"" pattern
    comma_quote = text.find(',"')
    if comma_quote > 0:
        return comma_quote

    # Fallback: walk comma-separated parts from the start and stop at
    # the first part that doesn't look like a name (first comma-separated
    # token containing 3+ words or a lowercase-initial word).
    offset = 0
    for part in text.split(","):
        stripped = part.strip()
        if not stripped:
            offset += len(part) + 1
            continue
        words = stripped.split()
        # Non-name heuristics: 3+ words, or starts with lowercase, or
        # the first word is a known title keyword.
        looks_like_name = (
            len(words) <= 3
            and words[0][0].isupper()
        )
        if not looks_like_name:
            return offset if offset > 0 else min(len(text), int(len(text) * 0.3))
        offset += len(part) + 1
    return min(len(text), int(len(text) * 0.3))


def _looks_like_author_section(text: str) -> bool:
    """Check if text looks like it contains author names."""
    # Should contain comma-separated names or single name
    # Quick heuristic: has at least one comma and uppercase letters
    return bool(re.search(r"[A-Z].*,", text)) or len(text) < 60


# ---------------------------------------------------------------------------
# Year extraction (Kurallar Rules 3, 4, 5, 6)
# ---------------------------------------------------------------------------

_NON_YEAR_DIGIT_MASKS = (
    re.compile(r"https?://\S+"),
    re.compile(r"10\.\d{4,9}/\S+"),
    re.compile(r"\barXiv\s*:\s*\d{4}\.\d{4,5}(?:v\d+)?\b", re.IGNORECASE),
    re.compile(r"\b\d{4}\.\d{4,5}(?:v\d+)?\b"),  # bare arXiv-id form
    re.compile(r"\b(\d{3,5})\s*[-‐-―]\s*(\d{1,5})\b"),  # page ranges
    re.compile(r"\bpp?\.\s*\d+", re.IGNORECASE),
    re.compile(
        r"\b(?:no|vol|volume|issue|num|number|month|cilt|sayi)\.?\s+[A-Za-zÀ-ɏ]+\s+\d{4}\b",
        re.IGNORECASE,
    ),
)


def _mask_non_year_digits(text: str) -> str:
    """Blank out digit clusters that are not citation-year candidates.

    URLs, DOIs, arXiv IDs, page ranges, ``pp. NNNN`` and ``no. May 2023``-style
    issue tags can otherwise leak 4-digit numbers into the year search.
    Masking with same-length spaces preserves character offsets so callers
    can still use returned positions against the original text.
    """
    out = text
    for pat in _NON_YEAR_DIGIT_MASKS:
        out = pat.sub(lambda m: " " * len(m.group(0)), out)
    return out


def _extract_year(text: str, author_end_pos: int) -> tuple[int | None, int, int]:
    """Extract year using Kurallar priority rules.

    Returns (year, start_pos, end_pos) in the original text.
    """
    # Mask digit clusters that look like years but aren't (DOI/arXiv/pages).
    # Same-length replacement preserves all match offsets.
    masked = _mask_non_year_digits(text)

    # Rule 5 (Definite): Parenthesized year takes absolute priority.
    # Accepts: "(2013)", "(2020a)", "(2013, 22 Aralik)", "(Mar. 2024)",
    # "(October 5, 2023)". Returns the full "(...)" span so title extraction
    # starts AFTER it.
    paren_year_with_tail = re.search(
        r"\("
        r"(?:[A-Za-zÀ-ɏ]+\.?\s+)?"                    # optional month abbrev/full
        r"(?:\d{1,2}[.,]?\s+)?"                       # optional day-of-month
        r"(\d{4})[a-z]?"                              # year (with optional disambiguation letter)
        r"(?:\s*,[^)]*)?"                             # optional trailing tail (", 22 Aralik")
        r"\s*\)",
        masked,
        re.IGNORECASE,
    )
    if paren_year_with_tail:
        year_val = int(paren_year_with_tail.group(1))
        if is_valid_year(year_val):
            return year_val, paren_year_with_tail.start(), paren_year_with_tail.end()

    # Vancouver-tail: "YYYY;Volume(Issue):Pages" — the canonical Vancouver
    # journal-volume separator. Runs before Rule 3 because a Vancouver ref
    # like "Authors, et al. <Title-with-leading-year>. Journal. YYYY;V(I):P"
    # would otherwise let Rule 3 match the in-title year via "et al. YYYY".
    # The "<digit><semicolon><digit>" shape is highly specific to Vancouver
    # and effectively never collides with other formats once URLs/DOIs are
    # masked out above.
    vanc_tail = re.search(r"\b(\d{4})\s*;\s*\d", masked)
    if vanc_tail:
        year_val = int(vanc_tail.group(1))
        if is_valid_year(year_val):
            return year_val, vanc_tail.start(1), vanc_tail.end(1)

    # Rule 3 (Definite): Year after &, and, ve, et al.
    conj_year = re.search(
        r"(?:&|and|ve|et\s+al\.)\s*,?\s*(\d{4})\b", masked, re.IGNORECASE
    )
    if conj_year:
        year_val = int(conj_year.group(1))
        if is_valid_year(year_val):
            return year_val, conj_year.start(1), conj_year.end(1)

    # Rule 4 (Definite): After conjunctions, if ( follows, year = 4 digits inside (
    conj_paren = re.search(
        r"(?:&|and|ve|et\s+al\.)\s*\((\d{4})", masked, re.IGNORECASE
    )
    if conj_paren:
        year_val = int(conj_paren.group(1))
        if is_valid_year(year_val):
            return year_val, conj_paren.start(1), conj_paren.end(1)

    # IEEE-tail: ", YYYY, doi:" / ", YYYY, URL:" / ", YYYY." at end of metadata.
    # IEEE refs (`..., vol. V, no. N, pp. X-Y, YYYY, doi: ...`) put the
    # publication year right before `doi:`/`URL:`. Anchor on that to beat
    # earlier issue/month-year fragments like `no. May 2023`.
    ieee_tail = re.search(
        r",\s*(\d{4})\s*(?:,\s*(?:doi|url)\b|\.\s*$|\s*$)",
        masked,
        re.IGNORECASE,
    )
    if ieee_tail:
        year_val = int(ieee_tail.group(1))
        if is_valid_year(year_val):
            return year_val, ieee_tail.start(1), ieee_tail.end(1)

    # Bare-year between sentence punctuation (APA-Turkish / Vancouver-bare):
    # `Authors. YYYY. Title` or `Authors, YYYY, Title`, optionally with a
    # disambiguation letter (`2017a.`). Preferred over Rule 6 because format
    # detection sometimes misclassifies these as IEEE and advances the
    # author boundary past the year.
    bare_re = re.compile(r"(?:^|[.,])\s+(\d{4})[a-z]?\s*[.,]\s", flags=re.IGNORECASE)
    bare_after = bare_re.search(masked, author_end_pos)
    if bare_after:
        year_val = int(bare_after.group(1))
        if is_valid_year(year_val):
            return year_val, bare_after.start(1), bare_after.end(1)
    bare_anywhere = bare_re.search(masked)
    if bare_anywhere:
        year_val = int(bare_anywhere.group(1))
        if is_valid_year(year_val):
            return year_val, bare_anywhere.start(1), bare_anywhere.end(1)

    # Rule 6 (Definite): First 4-digit number after author_end_pos. Once found, stop.
    after_authors = masked[author_end_pos:]
    first_year = YEAR_PATTERN.search(after_authors)
    if first_year:
        year_val = int(first_year.group(1))
        if is_valid_year(year_val):
            abs_start = author_end_pos + first_year.start(1)
            abs_end = author_end_pos + first_year.end(1)
            return year_val, abs_start, abs_end

    # Fallback: first year anywhere in text
    fallback = YEAR_PATTERN.search(masked)
    if fallback:
        year_val = int(fallback.group(1))
        if is_valid_year(year_val):
            return year_val, fallback.start(1), fallback.end(1)

    return None, -1, -1


# ---------------------------------------------------------------------------
# Title & Journal extraction (format-aware, Kurallar Title/Journal rules)
# ---------------------------------------------------------------------------

def _extract_title_journal(
    text: str,
    author_end: int,
    year_start: int,
    year_end: int,
    fmt: CitationFormat | None,
) -> tuple[str, str | None]:
    """Extract title and journal using format-aware parsing.

    Kurallar Title Rule 1: If in quotes, extract quoted content.
    Kurallar Journal Rule 2: Journal comes after comma.
    Kurallar Journal Rule 5: Period after journal = end of journal name.
    """
    if fmt in (CitationFormat.APA, CitationFormat.HARVARD):
        return _extract_title_journal_apa_harvard(text, author_end, year_start, year_end)
    if fmt in (CitationFormat.MLA, CitationFormat.CHICAGO):
        return _extract_title_journal_mla_chicago(text, author_end, year_start, year_end)
    if fmt == CitationFormat.VANCOUVER:
        return _extract_title_journal_vancouver(text, author_end, year_start, year_end)
    if fmt == CitationFormat.IEEE:
        return _extract_title_journal_ieee(text, author_end, year_start, year_end)

    return _extract_title_journal_unknown_format(text, author_end, year_start, year_end)


def _extract_title_journal_apa_harvard(
    text: str, author_end: int, year_start: int, year_end: int
) -> tuple[str, str | None]:
    """APA: Authors (Year). Title. Journal, Issue.
    Harvard: Authors Year. Title. Journal, Issue.
    Title comes AFTER year.
    """
    if year_end < 0:
        return _extract_title_journal_unknown_format(text, author_end, year_start, year_end)

    after_year = text[year_end:].strip().lstrip(".)]").strip()
    if not after_year:
        return "", None

    # Kurallar Title Rule 1: check for quoted title
    quoted = re.match(r'["\u201C]([^"\u201D]+)["\u201D]', after_year)
    if quoted:
        title = quoted.group(1).strip()
        remainder = after_year[quoted.end():].strip().lstrip(".,").strip()
        journal = _extract_journal_from_remainder(remainder)
        return title, journal

    # Title ends at first ". " followed by a capital letter (= journal start)
    parts = re.split(r"\.\s+(?=[A-Z])", after_year, maxsplit=1)
    title = parts[0].strip().rstrip(".")
    if len(parts) > 1:
        journal = _extract_journal_from_remainder(parts[1])
    else:
        journal = None

    return title, journal


def _extract_title_journal_mla_chicago(
    text: str, author_end: int, year_start: int, year_end: int
) -> tuple[str, str | None]:
    """MLA: Authors. "Title." Journal (Year): Issue.
    Chicago: Authors. "Title." Journal (Year): Issue.
    Title comes BEFORE year, after authors.
    """
    # The region between author_end and the year contains title + journal
    if year_start > author_end:
        between = text[author_end:year_start]
    else:
        between = text[author_end:]

    between = between.strip().lstrip(".,").strip()

    # If `between` has effectively no content, the source is APA-Springer
    # (`Authors (Year). Title. Journal`) misclassified as MLA/Chicago \u2014 title
    # is AFTER the year, not before. Defer to the APA handler.
    if len(between) < 5 and year_end >= 0:
        return _extract_title_journal_apa_harvard(text, author_end, year_start, year_end)

    # Kurallar Title Rule 1: quoted title
    quoted = re.search(r'["\u201C]([^"\u201D]+)["\u201D]', between)
    if quoted:
        title = quoted.group(1).strip().rstrip(".")
        # Journal is between the end of the quote and the year
        after_quote = between[quoted.end():].strip().lstrip(".,").strip()
        journal = _extract_journal_from_remainder(after_quote) if after_quote else None
        return title, journal

    # No quotes: split by period
    parts = re.split(r"\.\s+(?=[A-Z])", between, maxsplit=1)
    title = parts[0].strip().lstrip(".,").strip().rstrip(".")
    if len(parts) > 1:
        journal = _extract_journal_from_remainder(parts[1])
    else:
        journal = None

    return title, journal


def _extract_title_journal_vancouver(
    text: str, author_end: int, year_start: int, year_end: int
) -> tuple[str, str | None]:
    """Vancouver: Authors. Title. Journal. Year;Issue.
    Title and journal are period-separated, both before year.
    """
    if year_start > author_end:
        between = text[author_end:year_start]
    else:
        between = text[author_end:]

    between = between.strip().lstrip(".,").strip()

    # Split by ". " — first segment is title, second is journal
    parts = re.split(r"\.\s+", between, maxsplit=2)
    # Drop leading parts that are author-list continuation rather than the
    # title: Vancouver boundary detection sometimes stops mid-list and
    # leaves `Moons KG, et al` as parts[0].
    while parts and re.search(r"\bet\s+al\.?\s*$", parts[0].strip(), re.IGNORECASE):
        parts.pop(0)
    title = parts[0].strip().rstrip(".") if parts else ""
    journal = parts[1].strip().rstrip(".") if len(parts) > 1 else None

    return title, journal


def _extract_title_journal_ieee(
    text: str, author_end: int, year_start: int, year_end: int
) -> tuple[str, str | None]:
    """IEEE: Authors, "Title," Journal, vol. Issue, Year.
    Title is in quotes (ending with ,"), journal follows.
    """
    after_authors = text[author_end:].strip().lstrip(",").strip()

    # Kurallar Title Rule 1 + IEEE specific: title in quotes ending with ,"
    quoted = re.search(r'["\u201C]([^"\u201D]+)["\u201D]', after_authors)
    if quoted:
        title = quoted.group(1).strip().rstrip(",").rstrip(".")
        remainder = after_authors[quoted.end():].strip().lstrip(",").strip()
        journal = _extract_journal_from_remainder(remainder)
        return title, journal

    # Fallback
    return _extract_title_journal_unknown_format(text, author_end, year_start, year_end)


def _extract_title_journal_unknown_format(
    text: str, author_end: int, year_start: int, year_end: int
) -> tuple[str, str | None]:
    """Format-agnostic title/journal extraction used when the citation format is unknown or when a format-specific extractor has insufficient signal."""
    # Try parenthesized year pattern: Authors (Year). Title. Journal
    year_paren_match = re.search(r"\((?:19|20)\d{2}[a-z]?\)", text)
    if year_paren_match:
        after_year = text[year_paren_match.end():].strip().lstrip(".").strip()
        # Check for quoted title
        title_match = re.match(r'"([^"]+)"', after_year)
        if title_match:
            title = title_match.group(1).strip()
            remainder = after_year[title_match.end():].strip().lstrip(".").strip()
            journal = _extract_journal_from_remainder(remainder)
            return title, journal
        # Title ends at ". " + capital letter
        parts = re.split(r"\.\s+(?=[A-Z])", after_year, maxsplit=1)
        title = parts[0].strip().rstrip(".")
        journal = _extract_journal_from_remainder(parts[1]) if len(parts) > 1 else None
        return title, journal

    # Try quoted title anywhere — also harvest the journal from the text
    # AFTER the closing quote (covers `Authors. YYYY. "Title". Journal, ...`
    # bare-year forms where no `(YYYY)` paren is present).
    quote_match = re.search(r'["“]([^"”]+)["”]', text)
    if quote_match:
        title = quote_match.group(1).strip()
        remainder = text[quote_match.end():].strip().lstrip(".,").strip()
        journal = _extract_journal_from_remainder(remainder) if remainder else None
        return title, journal

    # Last resort: take text after author boundary, capped.
    # Strip leading bare year + optional disambiguation letter ("2017a.",
    # "2011a.") explicitly — the lstrip charset that follows would leave
    # the trailing letter behind ("a") and treat it as the title.
    after = text[author_end:].strip()
    after = re.sub(r"^[.,()]*\s*(?:19|20)\d{2}[a-z]?[.,]?\s*", "", after)
    after = after.lstrip(".,()0123456789 ").strip()
    if after:
        # Split at first period for title
        parts = re.split(r"\.\s+", after, maxsplit=1)
        title = parts[0].strip().rstrip(".")[:200]
        journal = _extract_journal_from_remainder(parts[1]) if len(parts) > 1 else None
        return title, journal

    return text[:200], None


_JOURNAL_LEADING_PREFIX_RE = re.compile(
    r"^(?:In|En|Da|De)\s*:\s*", flags=re.IGNORECASE
)
# Reject candidates that are just metadata labels — `URL`, `DOI: 10`, `arXiv:`,
# bare URLs/DOIs. These appear after title extraction when the reference has
# no real venue (arXiv-only, DOI-only, dataset).
_JOURNAL_REJECT_PREFIX_RE = re.compile(
    r"^\s*(?:arXiv|DOI|URL|https?:|10\.\d|©)", flags=re.IGNORECASE
)
# End-of-journal markers: comma+vol/issue/page/digit, OR period+identifier
# section (DOI/URL/arXiv/©), OR final period at end of text. The period-then-
# identifier branch lets us keep internal abbreviation periods like
# `Proc. Computer Vision and Pattern Recognition (CVPR), IEEE.` together.
_JOURNAL_END_RE = re.compile(
    r",\s*(?:vol\.?|pp\.?|p\.?|no\.?|issue|cilt|sayi|\d)"
    r"|\.\s*(?:DOI|URL|arXiv|https?://|10\.\d{4}|©)"
    r"|\.\s*$",
    flags=re.IGNORECASE,
)
# Trailing volume / volume.issue token left attached to the journal name
# (e.g. `Sensors 22.8`, `Pattern Recognition 147`). Stripped after the
# end-marker step so journal names that intentionally end in a digit
# (rare) survive only if no end-marker fired.
_JOURNAL_TRAILING_VOL_RE = re.compile(r"\s+\d{1,4}(?:\.\d{1,4})?\s*$")
# Final-result rejection: after all stripping the journal is just a
# metadata label fragment (`URL`, `In`, `Trans`, `arXiv`).
_JOURNAL_LABEL_ONLY_RE = re.compile(
    r"^\s*(?:arXiv|DOI|URL|In|Trans)\s*:?\s*$", flags=re.IGNORECASE
)


def _extract_journal_from_remainder(text: str) -> str | None:
    """Extract journal name from remaining text after title.

    Kurallar Journal Rule 2: after comma.
    Kurallar Journal Rule 4: journal name up to issue number.
    Kurallar Journal Rule 5: period after journal = end.
    """
    if not text or len(text.strip()) < 3:
        return None

    text = text.strip()

    # Strip leading metadata prefix some formats emit before the journal,
    # e.g. Springer-style `In: Pattern Recognition 147, ...`.
    text = _JOURNAL_LEADING_PREFIX_RE.sub("", text)

    # Reject candidates that are pure metadata labels / URLs / DOIs / arXiv.
    if _JOURNAL_REJECT_PREFIX_RE.match(text):
        return None

    # Cut at end-of-journal marker, otherwise take the whole remainder.
    end = _JOURNAL_END_RE.search(text)
    journal = text[: end.start()] if end else text

    # Strip stray punctuation, then any URL/DOI fragments that survived.
    journal = journal.strip(",;: \t.")
    journal = re.sub(r"https?://\S+", "", journal).strip()
    journal = re.sub(r"doi[:\s]*10\.\S+", "", journal, flags=re.IGNORECASE).strip()

    # Trim trailing volume / volume.issue glued onto the journal name.
    journal = _JOURNAL_TRAILING_VOL_RE.sub("", journal).strip()

    if len(journal) < 3:
        return None
    if _JOURNAL_LABEL_ONLY_RE.match(journal):
        return None

    return journal.rstrip(".,;:") or None


# ---------------------------------------------------------------------------
# Author parsing (format-aware)
# ---------------------------------------------------------------------------

def _parse_authors(author_text: str, fmt: CitationFormat | None = None) -> list[str]:
    """Parse author string into list of individual author names."""
    if not author_text:
        return []

    if fmt == CitationFormat.VANCOUVER:
        return _parse_vancouver_authors(author_text)

    # IEEE and all remaining formats share the standard parser. Standard
    # handles native IEEE input ("G. Liu, K. Y. Lee") correctly AND pairs
    # "Last, Initials" when a source is mis-classified as IEEE.
    return _parse_standard_authors(author_text)


def _parse_vancouver_authors(text: str) -> list[str]:
    """Parse Vancouver-style: 'Shingjergji K, Iren D, Urlings C, Klemke R'

    No comma between last name and initials. No period after initials.
    """
    # Remove trailing "et al." or "et al"
    text = re.sub(r",?\s*et\s+al\.?\s*$", "", text, flags=re.IGNORECASE).strip()

    # Split by comma — each part is "LastName Initials"
    parts = [p.strip() for p in text.split(",") if p.strip()]
    authors = []
    for part in parts:
        part = part.strip().rstrip(".")
        if len(part) > 1:
            authors.append(part)
    return authors[:20]


def _parse_standard_authors(author_text: str) -> list[str]:
    """Parse standard author format (APA/MLA/Chicago/Harvard).

    Handles: "Author, A., Author, B., & Author, C."
             "Author, Alice, and Bob Author"
    """
    if not author_text:
        return []

    # Normalize conjunctions to commas
    author_text = normalize_author_conjunctions(author_text)

    # Remove "et al." and Turkish "vd" / "vd." / "diğerleri" (= "et al.")
    author_text = re.sub(r",?\s*et\s+al\.?\s*$", "", author_text, flags=re.IGNORECASE).strip()
    author_text = re.sub(r",?\s*vd\.?\s*$", "", author_text, flags=re.IGNORECASE).strip()
    author_text = re.sub(r",?\s*diğerleri\s*$", "", author_text, flags=re.IGNORECASE).strip()

    # Split by comma
    parts = [p.strip() for p in author_text.split(",") if p.strip()]

    # Reassemble "Last, Initials" or "Last, First" pairs
    # Kurallar Rule 12: ", K." = author; Rule 7: single letter + period = abbreviation
    authors = []
    # Latin Extended A/B (À-ɏ) covers diacritic letters used across
    # European citations: Czech (ř, š), Spanish (á, ú, ñ), Portuguese (ã, ç),
    # Hungarian (ő, ű), Polish (ł, ż), Scandinavian (å, ø), French (é, è),
    # German (ä, ö, ü), plus Turkish. Without it "Borovec, Jiří" splits
    # because "Jiří" doesn't match the lowercase class.
    upper_cls = r"A-ZÇĞİÖŞÜÀ-ɏ"
    lower_cls = r"a-zçğıöşüÀ-ɏ"
    # Recognize a complete "Surname Initial(s)" Vancouver-style entry so
    # we don't over-pair (e.g. "Savran A." shouldn't grab "Sankur B").
    complete_vanc_re = re.compile(
        rf"^[{upper_cls}][{lower_cls}]+(?:[-\s][{upper_cls}][{lower_cls}]+)*"
        rf"\s+[{upper_cls}](?:\.?[{upper_cls}]){{0,3}}\.?$"
    )
    i = 0
    while i < len(parts):
        part = parts[i]
        # If part is already "Surname Init" (Vancouver-complete), don't pair.
        if complete_vanc_re.match(part):
            authors.append(part)
            i += 1
            continue
        if i + 1 < len(parts):
            next_part = parts[i + 1].strip()
            # Check if next part is initials. Handles:
            #   - plain / multi:     "A.", "A. S.", "C. J. C. H."
            #   - hyphenated upper:  "R.-N.", "C.-Y."
            #   - compound given:    "A.-r." (Abdel-rahman), "A. u." (Aziz ul)
            #   - interleaved upper/lower: "C. d. S." (Brazilian), "J. P. d. S."
            is_initials = bool(re.match(
                rf"^[{upper_cls}]"
                rf"(?:[.\-\s]+(?:[{upper_cls}]|[{lower_cls}]+))*"
                rf"\.?$",
                next_part,
            ))
            # Check if next part is a given-name section:
            #   - plain first name: "John", "Alice"
            #   - hyphenated given with uppercase: "Ying-Chun", "Bao-Liang"
            #   - first name + middle initial: "Julian P.", "Buse N."
            #   - multi-word given: "Buse Nur"
            is_first_name = bool(re.match(
                rf"^[{upper_cls}][{lower_cls}]*(?:-[{upper_cls}{lower_cls}]+)?"
                rf"(?:\s+[{upper_cls}][{lower_cls}]*(?:-[{upper_cls}{lower_cls}]+)?)?\.?$",
                next_part,
            ))
            if is_initials or is_first_name:
                authors.append(f"{part}, {next_part}")
                i += 2
                continue
        if len(part) > 1:  # Skip lone single-char initials (keep 2-char surnames like "Fu", "Li")
            authors.append(part)
        i += 1

    return authors[:20]


# ---------------------------------------------------------------------------
# Parse confidence scoring
# ---------------------------------------------------------------------------

def _compute_parse_confidence(parsed: ParsedSource) -> float:
    """Compute confidence score for parsed fields.

    Higher confidence means we trust the structured fields more.
    Low confidence triggers raw-text fallback in verification.
    """
    score = 0.0

    # DOI is the strongest signal
    if parsed.doi:
        score += 0.40

    # Authors with meaningful names
    valid_authors = [a for a in parsed.authors if len(a.strip()) >= 2]
    if valid_authors:
        score += 0.20

    # Year in valid range
    if parsed.year and 1900 <= parsed.year <= 2030:
        score += 0.15

    # Title of reasonable length
    if len(parsed.title) >= 10:
        score += 0.15

    # Journal (journal/conference/publisher) present
    if parsed.journal and len(parsed.journal) >= 3:
        score += 0.10

    return min(round(score, 2), 1.0)
