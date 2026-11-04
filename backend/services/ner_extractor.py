"""Extract structured citation fields using SIRIS-Lab/citation-parser-ENTITY NER model."""

import asyncio
import logging
import re
from collections import defaultdict

from models.source import ParsedSource
from utils.doi_extractor import extract_arxiv_id, extract_doi
from utils.url_cleaner import clean_extracted_url, find_best_url

logger = logging.getLogger(__name__)

YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
DOI_RE = re.compile(r"10\.\d{4,9}/[^\s,;\"'}\]]+")
URL_RE = re.compile(r"https?://[^\s,;\"'}\]]+")


async def extract_fields_ner(raw_text: str) -> ParsedSource | None:
    """Extract structured fields from raw source text using SIRIS citation parser.

    Returns None if the pipeline is unavailable or extraction fails.
    """
    from services.ner_model_manager import get_pipeline

    pipeline = await get_pipeline()
    if pipeline is None:
        return None

    try:
        from services.ner_model_manager import get_inference_executor
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            get_inference_executor(), _extract, pipeline, raw_text
        )
    except Exception as e:  # noqa: BLE001
        # Exception messages from ONNX Runtime can contain non-UTF8 bytes
        # (Windows system error text in the local codepage). Use repr() +
        # type name to guarantee the log message is a safe ASCII/UTF8
        # Python string.
        logger.warning("NER extraction failed: %s: %s", type(e).__name__, repr(e))
        return None


def _extract(pipeline, raw_text: str) -> ParsedSource:
    """Run the NER pipeline and map results to ParsedSource.

    Synchronous — always call via ``run_in_executor`` from async code so
    the CPU/GPU forward pass does not block the event loop.
    """
    raw_entities = pipeline(raw_text)

    # Group entities by label, using original text offsets
    by_label: dict[str, list[dict]] = defaultdict(list)
    for ent in raw_entities:
        label = ent["entity_group"]
        text = raw_text[ent["start"]:ent["end"]]
        by_label[label].append({
            "text": text,
            "score": ent["score"],
            "start": ent["start"],
            "end": ent["end"],
        })

    # --- Title ---
    title = _best_text(by_label.get("TITLE", []))

    # --- Authors ---
    authors = _parse_authors_from_raw(raw_text, by_label.get("AUTHORS", []))

    # --- Year --- (SIRIS uses PUBLICATION_YEAR label)
    year = _parse_year(by_label.get("PUBLICATION_YEAR", []) or by_label.get("YEAR", []), raw_text)

    # --- Source (journal/conference/publisher name) ---
    source = _best_text(by_label.get("JOURNAL", []))

    # --- Bibliographic extras (display-only, never feed into scoring).
    # Confidence floors are slightly above the default 0.3 because volume/
    # issue tokens are short numeric strings the model occasionally mis-tags
    # as ISSUE when they're actually authors' initials or stray digits.
    publisher = _best_text(by_label.get("PUBLISHER", []))
    volume = _best_text(by_label.get("VOLUME", []), min_score=0.5)
    issue = _best_text(by_label.get("ISSUE", []), min_score=0.5)
    pages = _parse_pages(by_label.get("PAGE_FIRST", []), by_label.get("PAGE_LAST", []))
    issn = _collect_short_codes(by_label.get("ISSN", []), min_score=0.5, limit=2)
    isbn = _collect_short_codes(by_label.get("ISBN", []), min_score=0.5, limit=2)

    # --- DOI --- filter noise (lone periods, short fragments)
    doi = _parse_doi(by_label.get("DOI", []), raw_text)

    # --- arXiv ID --- from DOI entities or raw text
    arxiv_id = extract_arxiv_id(raw_text)

    # --- URL --- priority: doi > arxiv > first extracted URL
    url = _build_url(doi, arxiv_id, by_label.get("LINK_ONLINE_AVAILABILITY", []), raw_text)

    # --- Confidence ---
    confidence = _compute_confidence(title, authors, year, doi, arxiv_id, source)

    return ParsedSource(
        raw_text=raw_text,
        title=title or "",
        authors=authors,
        year=year,
        url=url,
        journal=source,
        volume=volume,
        issue=issue,
        pages=pages,
        publisher=publisher,
        issn=issn,
        isbn=isbn,
        extraction_method="ner",
        parse_confidence=confidence,
    )


def _best_text(entities: list[dict], min_score: float = 0.3) -> str | None:
    """Pick the highest-scoring entity text, filtering low confidence."""
    valid = [e for e in entities if e["score"] >= min_score and len(e["text"].strip()) > 1]
    if not valid:
        return None
    best = max(valid, key=lambda e: e["score"])
    return best["text"].strip()


def _best_short_text(entities: list[dict], min_score: float) -> str | None:
    """Like ``_best_text`` but accepts single-character tokens — page numbers
    can legitimately be one digit."""
    valid = [e for e in entities if e["score"] >= min_score and e["text"].strip()]
    if not valid:
        return None
    best = max(valid, key=lambda e: e["score"])
    return best["text"].strip()


def _parse_pages(first_entities: list[dict], last_entities: list[dict]) -> str | None:
    """Combine PAGE_FIRST + PAGE_LAST entities into ``"first-last"`` (or just
    ``"first"`` when last is missing)."""
    first = _best_short_text(first_entities, min_score=0.4)
    last = _best_short_text(last_entities, min_score=0.4)
    if first and last and first != last:
        return f"{first}-{last}"
    return first or last


def _collect_short_codes(entities: list[dict], min_score: float, limit: int) -> list[str]:
    """De-duped list of high-confidence ISSN/ISBN-style code strings."""
    seen: set[str] = set()
    out: list[str] = []
    for e in entities:
        if e["score"] < min_score:
            continue
        code = e["text"].strip()
        if code and code not in seen:
            seen.add(code)
            out.append(code)
            if len(out) >= limit:
                break
    return out


def _parse_authors_from_raw(raw_text: str, entities: list[dict]) -> list[str]:
    """Parse author entities into individual author names.

    Uses original raw_text offsets to reconstruct the full author span,
    since SIRIS may split AUTHORS into multiple entity chunks around
    separators like '&', 'and', 've'.
    """
    if not entities:
        return []

    valid = [e for e in entities if e["score"] >= 0.3]
    if not valid:
        return []

    # Reconstruct full author text from the raw_text using the span
    # from the first entity's start to the last entity's end
    valid.sort(key=lambda e: e["start"])
    span_start = valid[0]["start"]
    span_end = valid[-1]["end"]
    full_text = raw_text[span_start:span_end].strip().rstrip(".(")

    if not full_text:
        return []

    # Normalize conjunctions to commas
    full_text = re.sub(r"\s*&\s*", ", ", full_text)
    full_text = re.sub(r"\s+and\s+", ", ", full_text, flags=re.IGNORECASE)
    full_text = re.sub(r"\s+ve\s+", ", ", full_text)  # Turkish "and"

    # Remove "et al."
    full_text = re.sub(r",?\s*et\s+al\.?\s*$", "", full_text, flags=re.IGNORECASE).strip()

    # Try splitting by semicolons first (some formats)
    if ";" in full_text:
        parts = [p.strip() for p in full_text.split(";") if p.strip()]
    else:
        parts = _split_authors_by_comma(full_text)

    authors = []
    for part in parts:
        part = part.strip().rstrip(".,")
        if len(part) >= 2:
            authors.append(part)

    return authors[:20]


def _split_authors_by_comma(text: str) -> list[str]:
    """Split comma-separated author string, keeping 'Last, Init.' pairs together."""
    # Filter empty parts from double-comma artifacts (e.g., "A., , B")
    parts = [p.strip() for p in text.split(",") if p.strip()]
    authors = []
    # Recognize "Surname Init(s)" Vancouver-complete entry to avoid over-pairing.
    complete_vanc_re = re.compile(
        r"^[A-Z\u00C0-\u024F][a-z\u00E0-\u024F]+"
        r"(?:[-\s][A-Z\u00C0-\u024F][a-z\u00E0-\u024F]+)*"
        r"\s+[A-Z\u00C0-\u024F](?:\.?[A-Z\u00C0-\u024F]){0,3}\.?$"
    )
    i = 0
    while i < len(parts):
        part = parts[i]
        if complete_vanc_re.match(part):
            authors.append(part)
            i += 1
            continue
        if i + 1 < len(parts):
            next_part = parts[i + 1].strip()
            # Next part is initials (1-4 letters, dots/hyphens optional)
            is_initials = bool(re.match(
                r"^[A-Z\u00C0-\u024F](?:[.\-\s]*[A-Z\u00C0-\u024F]){0,3}\.?$",
                next_part,
            ))
            # Next part is a given-name section (plain, hyphenated with
            # uppercase after hyphen like "Ying-Chun", or first + middle
            # initial like "Julian P.")
            is_first_name = bool(re.match(
                r"^[A-Z\u00C0-\u024F][a-z\u00E0-\u024F]*"
                r"(?:-[A-Za-z\u00C0-\u024F\u00E0-\u024F]+)?"
                r"(?:\s+[A-Z\u00C0-\u024F][a-z\u00E0-\u024F]*"
                r"(?:-[A-Za-z\u00C0-\u024F\u00E0-\u024F]+)?)?\.?$",
                next_part,
            ))
            if is_initials or is_first_name:
                authors.append(f"{part}, {next_part}")
                i += 2
                continue
        if len(part) > 1:
            authors.append(part)
        i += 1
    return authors


def _parse_year(entities: list[dict], raw_text: str) -> int | None:
    """Extract year from YEAR entities or fallback to regex.

    SIRIS occasionally splits a 4-digit year across adjacent subword
    entities (e.g. ``"2024"`` → ``"20"`` + ``"24"``); when the literal
    entity text fails the 4-digit regex, peek at the surrounding raw_text
    span to recover the full year. This also prevents page-range starts
    (``"1963"`` in ``"1963–1980"``) from winning over a fragmented
    publication year.
    """
    candidates: list[tuple[float, int, int]] = []  # (-score, start, year)
    for ent in entities:
        m = YEAR_RE.search(ent["text"])
        if m:
            val = int(m.group(1))
            if 1900 <= val <= 2099:
                candidates.append((-ent["score"], ent["start"], val))
                continue
        start = max(0, ent["start"] - 2)
        end = min(len(raw_text), ent["end"] + 2)
        m = YEAR_RE.search(raw_text[start:end])
        if m:
            val = int(m.group(1))
            if 1900 <= val <= 2099:
                candidates.append((-ent["score"], ent["start"], val))

    if candidates:
        candidates.sort()
        return candidates[0][2]

    m = YEAR_RE.search(raw_text)
    if m:
        val = int(m.group(1))
        if 1900 <= val <= 2099:
            return val
    return None


def _parse_doi(entities: list[dict], raw_text: str) -> str | None:
    """Extract DOI from DOI entities, filtering noise.

    Routes through `extract_doi`/`normalize_doi` so wrap-broken DOIs
    (`10.1038/s41598-023- 47595-7`) get rejoined instead of truncated.
    """
    for ent in sorted(entities, key=lambda e: -e["score"]):
        doi = extract_doi(ent.get("text") or "")
        if doi:
            return doi

    return extract_doi(raw_text)


def _build_url(doi: str | None, arxiv_id: str | None,
                link_entities: list[dict], raw_text: str) -> str | None:
    """Build the canonical URL: doi link > arxiv link > first extracted URL."""
    if doi:
        return f"https://doi.org/{doi}"
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"

    # Fall back to first URL from entities or regex. NER spans frequently
    # contain a literal space from a PDF line wrap, so route through the
    # cleaner. NER also sometimes truncates the span at a domain boundary
    # (`https://openaccess.thecvf` instead of the full path), so we always
    # also extract from raw text and prefer whichever is longer when one is
    # a prefix of the other.
    raw_url = find_best_url(raw_text)
    for ent in link_entities:
        cleaned = clean_extracted_url(ent.get("text"))
        if not cleaned:
            continue
        if raw_url and raw_url.startswith(cleaned) and len(raw_url) > len(cleaned):
            return raw_url
        return cleaned

    return raw_url


def _compute_confidence(
    title: str | None,
    authors: list[str],
    year: int | None,
    doi: str | None,
    arxiv_id: str | None,
    source: str | None,
) -> float:
    """Compute parse confidence from extracted fields."""
    score = 0.0

    if doi or arxiv_id:
        score += 0.40
    if authors:
        score += 0.20
    if year and 1900 <= year <= 2099:
        score += 0.15
    if title and len(title) >= 10:
        score += 0.15
    if source and len(source) >= 3:
        score += 0.10

    return min(round(score, 2), 1.0)
