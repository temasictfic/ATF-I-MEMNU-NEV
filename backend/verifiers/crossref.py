"""Crossref API verifier - DOI lookup and bibliographic search."""

import re
from itertools import combinations
from typing import Any
from urllib.parse import quote, quote_plus

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from scrapers.rate_limiter import rate_limiter
from services.match_scorer import score_match
from services.scoring_constants import (
    DOI_MATCH_MIN_SCORE,
    HIGH_PARSE_CONFIDENCE_THRESHOLD,
)
from services.search_settings import get_polite_pool_email

from verifiers._http import (
    build_headers,
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
)

CROSSREF_API = "https://api.crossref.org/works"
_HOST = "api.crossref.org"

# Crossref's December 2025 rate-limit regime caps list/query requests at
# 1 req/s (anonymous) and 3 req/s (polite pool with mailto). The polite
# ceiling is still tight for a variant-walking search — 0.35 s leaves
# headroom for Cloudflare's burst shaping without tripping 429s. The
# anonymous path defers to the limiter's conservative 1.0 s default.
_POLITE_POOL_PACE_SECONDS = 0.35


def _crossref_pace_seconds() -> float | None:
    """Return the per-request pacing to use for the current Crossref call.

    Returns ``None`` to let the rate limiter fall back to its host default
    (the anonymous-safe 1.0 s) when no contact email is configured. When
    the user has supplied a ``polite_pool_email`` — which our User-Agent
    already advertises as ``mailto:…`` — Crossref routes us into the polite
    pool and we can cadence at the faster 0.35 s.
    """
    return _POLITE_POOL_PACE_SECONDS if get_polite_pool_email() else None


async def search_by_doi(source: ParsedSource) -> MatchResult | None:
    """Direct DOI lookup via Crossref."""
    if not source.doi:
        return None

    session = get_session()
    url = f"{CROSSREF_API}/{quote(source.doi, safe='')}"
    check_parked_url(url)
    await rate_limiter.acquire(_HOST, rate=_crossref_pace_seconds())
    async with session.get(url, headers=build_headers()) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        item = data.get("message", {})
        return _item_to_match(item, source)


async def search(source: ParsedSource) -> MatchResult | None:
    """Search Crossref — tries DOI lookup first, then NER-title search.

    Every search is driven by the NER-extracted title. Author, container-title
    and year filters layer on for disambiguation so a re-edition of the same
    work cannot shadow the originally cited one.
    """
    # 1. DOI lookup takes priority — unambiguous when a DOI is present.
    if source.doi:
        doi_result = await search_by_doi(source)
        if doi_result and doi_result.score >= DOI_MATCH_MIN_SCORE:
            return doi_result

    # 2. Title-based query. Without a title there is nothing to search with.
    query = (source.title or "").strip()
    if not query:
        return None

    params: dict[str, str] = {
        "query.bibliographic": query,
        "query.title": query,
        "rows": "5",
    }

    # Add author disambiguation only when parsing is strong and citation
    # doesn't look editor-led (Ed./Eds.), where Crossref author filtering
    # can suppress the correct book-level record.
    author_query = _build_author_query(source.authors)
    if (
        author_query
        and source.parse_confidence >= HIGH_PARSE_CONFIDENCE_THRESHOLD
        and not _looks_like_editor_source(source.raw_text)
    ):
        params["query.author"] = author_query

    # Add container-title to separate editions published by different houses
    # under slightly different encyclopedia/journal titles.
    if source.journal and _is_specific_container_title(source.journal):
        params["query.container-title"] = source.journal

    # Year-range filter (±1 year) excludes papers from other editions whose
    # publication year differs from the cited one.  A tolerance of one year
    # absorbs common print-vs-online date discrepancies.
    if source.year:
        params["filter"] = (
            f"from-pub-date:{source.year - 1},until-pub-date:{source.year + 1}"
        )

    session = get_session()
    # Walk every subset of present {query.container-title, query.author,
    # filter} keys, dropping fewest first. The full-params variant runs
    # first; the bare-title fallback (all three dropped) runs last. Any
    # absent keys silently shrink the search space.
    optional_keys = ("query.container-title", "query.author", "filter")
    present = [k for k in optional_keys if k in params]
    variants: list[dict[str, str]] = []
    for size in range(len(present) + 1):
        for drop in combinations(present, size):
            drop_set = set(drop)
            variants.append({k: v for k, v in params.items() if k not in drop_set})

    # Walk the variant list, but break out as soon as a variant lands a
    # DOI-exact hit (score ≥ 0.95 with a URL/DOI match). That's the same
    # threshold the orchestrator uses to cancel sibling verifiers, and
    # it covers the common case where the very first variant already
    # returns a perfect match via Crossref's own relevance ranker. On
    # ambiguous sources where no variant crosses the bar, every
    # variant still runs and the best scoring result wins — exactly the
    # behaviour we had before, just skipped for the easy cases.
    best: MatchResult | None = None
    for variant in variants:
        result = await _fetch_best_match(session, variant, source)
        if result and (best is None or result.score > best.score):
            best = result
        if best and best.score >= 0.95 and best.match_details.url_match:
            break

    return best


async def _fetch_best_match(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
) -> MatchResult | None:
    """Execute one Crossref API request and return the highest-scoring match."""
    check_parked_url(CROSSREF_API)
    await rate_limiter.acquire(_HOST, rate=_crossref_pace_seconds())
    async with session.get(CROSSREF_API, params=params, headers=build_headers()) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        items = data.get("message", {}).get("items", [])

        best: MatchResult | None = None
        for item in items[:5]:
            match = _item_to_match(item, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _item_to_match(item: dict[str, Any], source: ParsedSource) -> MatchResult | None:
    """Convert a Crossref work item to a MatchResult."""
    title_parts = item.get("title", [])
    title = title_parts[0] if title_parts else ""

    authors = []
    for author in item.get("author", []):
        name = f"{author.get('family', '')}, {author.get('given', '')}".strip(", ")
        if name:
            authors.append(name)

    # Year-extraction fallback chain: print → online → published → issued.
    # Many records — particularly books, datasets, and preprints — only
    # populate ``issued`` (the canonical Crossref date) and not the
    # print/online split, so we'd lose the year and miss the year-bonus
    # in scoring. ``published`` is the Crossref-computed best-of date.
    year = None
    for key in ("published-print", "published-online", "published", "issued"):
        date_parts = (item.get(key) or {}).get("date-parts") or [[]]
        if date_parts and date_parts[0]:
            year = date_parts[0][0]
            break

    doi = item.get("DOI", "")

    journal_parts = item.get("container-title", [])
    journal = journal_parts[0] if journal_parts else ""

    editors: list[str] = []
    for ed in item.get("editor", []) or []:
        if not isinstance(ed, dict):
            continue
        name = f"{ed.get('family', '')}, {ed.get('given', '')}".strip(", ")
        if name:
            editors.append(name)

    issn_list = [s for s in (item.get("ISSN") or []) if isinstance(s, str) and s]
    isbn_list = [s for s in (item.get("ISBN") or []) if isinstance(s, str) and s]

    candidate = {
        "database": "Crossref",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": f"https://doi.org/{doi}" if doi else "",
        "search_url": f"https://search.crossref.org/search/works?q={quote_plus(source.title or source.raw_text[:100])}&from_ui=yes",
        "volume": item.get("volume") or None,
        "issue": item.get("issue") or None,
        "pages": item.get("page") or None,
        "publisher": item.get("publisher", "") or "",
        "editor": editors,
        "document_type": item.get("type", "") or "",
        "language": item.get("language", "") or "",
        "issn": issn_list,
        "isbn": isbn_list,
    }

    return score_match(source, candidate)


def _build_author_query(authors: list[str]) -> str:
    """Build a Crossref-friendly author query from parsed authors."""
    if not authors:
        return ""

    names: list[str] = []
    seen: set[str] = set()
    for author in authors:
        family = _extract_family_name(author)
        if not family:
            continue
        key = family.lower()
        if key in seen:
            continue
        seen.add(key)
        names.append(family)
        if len(names) >= 2:
            break

    return " ".join(names)


def _extract_family_name(author: str) -> str:
    """Extract a likely family name from an author token."""
    raw = (author or "").strip().strip(",.")
    if not raw:
        return ""
    if raw.lower() in {"ed", "eds", "editor", "editors"}:
        return ""

    if "," in raw:
        candidate = raw.split(",", 1)[0].strip()
    else:
        # Initials-first style: "A. Bajaj" -> "Bajaj"
        initials_first = re.match(r"^(?:[A-Z]\.?(?:\s+|$))+([A-Z][A-Za-z'\-]+)$", raw)
        if initials_first:
            candidate = initials_first.group(1).strip()
        else:
            parts = [p for p in re.split(r"\s+", raw) if p]
            candidate = parts[-1] if parts else ""

    candidate = re.sub(r"[^A-Za-z\-']", "", candidate).strip("-'")
    if len(candidate) < 2:
        return ""
    return candidate


def _looks_like_editor_source(raw_text: str) -> bool:
    """Detect book/editor citations where author filtering is unreliable."""
    text = (raw_text or "").lower()
    return bool(re.search(r"\b(?:ed\.|eds\.|editor|editors)\b", text))


def _is_specific_container_title(journal: str) -> bool:
    """Return False for short/generic container titles that over-filter results."""
    value = (journal or "").strip()
    if not value:
        return False

    normalized = re.sub(r"[^a-z]", "", value.lower())
    if len(normalized) < 6:
        return False

    generic = {
        "proc",
        "proceedings",
        "conference",
        "conf",
        "journal",
        "book",
    }
    return normalized not in generic
