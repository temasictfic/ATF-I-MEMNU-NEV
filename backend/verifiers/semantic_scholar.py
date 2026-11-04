"""Semantic Scholar API verifier."""

import time
from typing import Any
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from scrapers.rate_limiter import rate_limiter
from services.match_scorer import score_match

from verifiers._http import (
    RateLimitedError,
    UnauthorizedError,
    check_parked_url,
    check_rate_limit,
    fetch_with_year_fallback,
    get_session,
    raise_for_unexpected_status,
)

API_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
_HOST = "api.semanticscholar.org"

# When the anonymous shared pool throttles us, S2 frequently omits the
# Retry-After header. The default 10 s park (in _http.check_rate_limit) is
# too short for the 5-minute global bucket the shared pool resets on, so a
# rapid retry just trips the same 429 again — and repeat 429s on the same
# IP are the pattern that gets keys flagged. Treat the floor as 5 minutes
# to match the documented bucket window.
_ANON_PARK_FLOOR_SECONDS = 300.0

# Cooldown after S2 rejects the supplied x-api-key so we don't spam denied
# requests at their auth endpoint.
_UNAUTHORIZED_COOLDOWN_SEC = 3600.0
_unauthorized_until: float = 0.0
_UNAUTHORIZED_HINT = (
    "Semantic Scholar rejected the API key. Update it in Settings → API Keys."
)


async def search(source: ParsedSource, api_key: str | None = None) -> MatchResult | None:
    """Search Semantic Scholar by title only.

    Title-only queries produce better, more targeted results.  The extracted
    fields (authors, year, journal) are used for *scoring*, not querying.
    The ``year`` parameter restricts results to a ±1-year window when known.
    Optional ``api_key`` adds an ``x-api-key`` header for higher rate limits.
    """
    query = source.title
    if not query:
        return None

    params: dict[str, str] = {
        "query": query,
        "limit": "5",
        "fields": "title,authors,year,externalIds,url,venue,journal,publicationTypes",
    }

    # Semantic Scholar Graph API supports year=YYYY-YYYY range filtering.
    if source.year:
        params["year"] = f"{source.year - 1}-{source.year + 1}"

    headers: dict[str, str] = {}
    if api_key:
        headers["x-api-key"] = api_key

    session = get_session()
    return await fetch_with_year_fallback(
        lambda p: _fetch_best_match(session, p, source, headers),
        params,
        {"year"},
    )


async def _fetch_best_match(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
    headers: dict[str, str] | None = None,
) -> MatchResult | None:
    """Execute one S2 request and return the highest-scoring match."""
    global _unauthorized_until
    has_key = bool(headers and headers.get("x-api-key"))
    now = time.monotonic()
    if has_key and now < _unauthorized_until:
        raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT)

    check_parked_url(API_URL)
    await rate_limiter.acquire(_HOST)
    async with session.get(API_URL, params=params, headers=headers or {}) as resp:
        try:
            check_rate_limit(resp)
        except RateLimitedError:
            # Anonymous (no x-api-key) means the shared 5k/5min pool —
            # extend the park to 5 min so a rapid retry doesn't re-hit
            # the same exhausted bucket. Keyed users hit the per-user
            # 1 rps cap which recovers in seconds, so leave the default
            # park behavior for them.
            if not has_key:
                rate_limiter.park(_HOST, _ANON_PARK_FLOOR_SECONDS)
            raise
        # Only treat 401/403 as an auth error when a key was supplied; an
        # anonymous 401 would be a server-side anomaly, not something the
        # user can fix by updating settings.
        if has_key and resp.status in (401, 403):
            _unauthorized_until = now + _UNAUTHORIZED_COOLDOWN_SEC
            raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT, status=resp.status)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        papers = data.get("data", [])

        best: MatchResult | None = None
        for paper in papers[:5]:
            match = _paper_to_match(paper, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _paper_to_match(paper: dict[str, Any], source: ParsedSource) -> MatchResult | None:
    title = paper.get("title", "")
    authors = [a.get("name", "") for a in paper.get("authors", []) if a.get("name")]
    year = paper.get("year")

    ext_ids = paper.get("externalIds", {}) or {}
    doi = ext_ids.get("DOI", "")

    # Prefer a DOI-based URL (direct paper link) over the S2 paper page.
    url = f"https://doi.org/{doi}" if doi else paper.get("url", "")

    journal_obj = paper.get("journal") or {}
    if not isinstance(journal_obj, dict):
        journal_obj = {}
    volume = journal_obj.get("volume") or None
    pages = journal_obj.get("pages") or None

    pub_types = paper.get("publicationTypes") or []
    document_type = ""
    if isinstance(pub_types, list) and pub_types:
        document_type = str(pub_types[0]) if pub_types[0] else ""

    search_query = source.title or (source.raw_text[:100] if source.raw_text else "")
    candidate = {
        "database": "Semantic Scholar",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": paper.get("venue", ""),
        "url": url,
        "search_url": f"https://www.semanticscholar.org/search?q={quote(search_query)}",
        "volume": volume,
        "pages": pages,
        "document_type": document_type,
    }

    return score_match(source, candidate)
