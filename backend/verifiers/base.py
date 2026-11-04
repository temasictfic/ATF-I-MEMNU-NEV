"""BASE (Bielefeld Academic Search Engine) verifier.

Implements the BASE HTTP Interface v1.29 (April 2026):
https://www.base-search.net/about/download/base_interface.pdf

BASE indexes ~470M documents from 12,100+ open-access repositories and is
particularly strong on non-English / repository-only sources that Crossref,
PubMed, and the other Tier-1 verifiers miss.

Auth: ``apikey`` is **mandatory** per the spec. BASE issues keys on
request via https://www.base-search.net/about/en/contact.php (select
"Access BASE's HTTP API"). BASE support has confirmed the key is the
sole credential — IP allowlisting is not required. The misleading
``"Access denied for IP address ..."`` body BASE returns on key-less
requests is documented in the spec's error list (page 13) and means
"no usable key", not "your IP is blocked".

Spec-imposed limits this verifier respects:
- ``query``: max 1000 characters
- ``hits``: max 120 (we use 5)
- 1 request per second (handled by ``rate_limiter``)

The year filter goes inside the ``query`` (e.g. ``dcyear:[2020 TO 2024]``)
because the spec lists no separate ``filter=`` parameter for PerformSearch.
"""

import time
from typing import Any
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from scrapers.rate_limiter import rate_limiter
from services.match_scorer import score_match

from verifiers._http import (
    UnauthorizedError,
    UpstreamError,
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
    strip_phrase_chars,
)

BASE_API = "https://api.base-search.net/cgi-bin/BaseHttpSearchInterface.fcgi"
_HOST = "api.base-search.net"
_MAX_QUERY_CHARS = 1000  # spec limit on the ``query`` parameter

# Once BASE rejects this process for IP/auth, suppress further outbound
# requests for an hour so we don't hammer their API on every source.
# Surfaces UnauthorizedError immediately during the cooldown.
_UNAUTHORIZED_COOLDOWN_SEC = 3600.0
_unauthorized_until: float = 0.0


def _coerce_str(value: Any) -> str:
    """Return the first string in a list, or the value itself when string."""
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _coerce_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if x]
    if isinstance(value, str) and value:
        return [value]
    return []


async def search(source: ParsedSource, api_key: str | None = None) -> MatchResult | None:
    """Search BASE by title (and first-author surname when available).

    Builds a SOLR-style query against ``dctitle`` (and optionally ``dccreator``
    and a ``dcyear:[..]`` range), bounded to ``_MAX_QUERY_CHARS``. Tries the
    year-restricted variant first; if that returns no hits, retries once
    without the year restriction.
    """
    if not api_key:
        # ``apikey`` is mandatory per the spec — without it BASE returns the
        # "Access denied" body. Skip the round-trip; orchestrator paints
        # ``no_match`` like any other empty result.
        return None

    title = strip_phrase_chars(source.title or "")
    if not title:
        return None

    # Solr phrase query on dctitle. Add a dccreator filter when at least one
    # parsed author is available — keeps the result set targeted on common
    # titles like "Introduction".
    query_parts = [f'dctitle:"{title}"']
    if source.authors:
        first = strip_phrase_chars((source.authors[0] or "").split(",")[0])
        if first and len(first) > 1:
            query_parts.append(f'dccreator:"{first}"')
    base_query = " AND ".join(query_parts)
    if len(base_query) > _MAX_QUERY_CHARS:
        # Title alone exceeds the spec's 1000-char ceiling. Drop the request
        # rather than send a query BASE will reject with 400.
        return None

    query_with_year = base_query
    if source.year:
        candidate = f"{base_query} AND dcyear:[{source.year - 1} TO {source.year + 1}]"
        if len(candidate) <= _MAX_QUERY_CHARS:
            query_with_year = candidate

    base_params: dict[str, str] = {
        "func": "PerformSearch",
        "format": "json",
        "hits": "5",
        "apikey": api_key,
    }

    session = get_session()
    result = await _fetch_best_match(
        session, {**base_params, "query": query_with_year}, source
    )
    if result is None and query_with_year != base_query:
        result = await _fetch_best_match(
            session, {**base_params, "query": base_query}, source
        )
    return result


_UNAUTHORIZED_HINT = (
    "BASE rejected the request. Verify your API key in Settings -> API Keys. "
    "If you don't have one, request access at "
    "https://www.base-search.net/about/en/contact.php (\"Access BASE's HTTP API\")."
)


async def _fetch_best_match(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
) -> MatchResult | None:
    """Execute one BASE request and return the highest-scoring hit."""
    global _unauthorized_until
    now = time.monotonic()
    if now < _unauthorized_until:
        raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT)

    check_parked_url(BASE_API)
    await rate_limiter.acquire(_HOST)
    async with session.get(BASE_API, params=params) as resp:
        check_rate_limit(resp)
        if resp.status in (401, 403):
            _unauthorized_until = now + _UNAUTHORIZED_COOLDOWN_SEC
            raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT, status=resp.status)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as e:
            raise UpstreamError(_HOST, 200, f"invalid JSON: {e}") from e

        # BASE returns 200 with a top-level ``error`` key (and no ``response``)
        # for any spec-listed error — see the table on page 13 of the
        # interface guide. "Access denied" maps to UnauthorizedError; the
        # rest (malformed query, missing func, repository not found, system
        # maintenance, ...) get UpstreamError so the user sees the real
        # cause instead of a misleading no_match.
        if isinstance(data, dict) and "response" not in data and data.get("error"):
            err = str(data.get("error", "")).strip()
            err_lower = err.lower()
            if "access denied" in err_lower or "ip address" in err_lower:
                _unauthorized_until = now + _UNAUTHORIZED_COOLDOWN_SEC
                raise UnauthorizedError(_HOST, detail=err or _UNAUTHORIZED_HINT, status=200)
            raise UpstreamError(_HOST, 200, err or "BASE returned an error body")

        docs = (data.get("response") or {}).get("docs") or []
        best: MatchResult | None = None
        for doc in docs[:5]:
            match = _doc_to_match(doc, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _doc_to_match(doc: dict[str, Any], source: ParsedSource) -> MatchResult | None:
    title = _coerce_str(doc.get("dctitle"))
    if not title:
        return None

    authors = _coerce_list(doc.get("dccreator"))

    year_raw = _coerce_str(doc.get("dcyear"))
    year: int | None = None
    if year_raw:
        try:
            year = int(year_raw[:4])
        except ValueError:
            year = None

    doi = _coerce_str(doc.get("dcdoi"))
    journal = _coerce_str(doc.get("dcsource"))

    # dclink can be a string (sometimes ;-separated) or a list of URLs.
    link_raw = doc.get("dclink") or doc.get("dcidentifier") or ""
    if isinstance(link_raw, list) and link_raw:
        url = str(link_raw[0])
    elif isinstance(link_raw, str):
        url = link_raw.split(";")[0].strip()
    else:
        url = ""
    if not url and doi:
        url = f"https://doi.org/{doi}"

    publisher = _coerce_str(doc.get("dcpublisher"))
    document_type = _coerce_str(doc.get("dctype"))
    language = _coerce_str(doc.get("dclanguage"))
    issn_list = _coerce_list(doc.get("dcissn"))
    isbn_list = _coerce_list(doc.get("dcisbn"))

    search_query = source.title or (source.raw_text[:100] if source.raw_text else "")
    candidate = {
        "database": "BASE",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": f"https://www.base-search.net/Search/Results?lookfor={quote(search_query)}",
        "publisher": publisher,
        "document_type": document_type,
        "language": language,
        "issn": issn_list,
        "isbn": isbn_list,
    }
    return score_match(source, candidate)
