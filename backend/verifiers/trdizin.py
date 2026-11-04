"""TRDizin verifier - Turkish national academic database (JSON API)."""

from typing import Any
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from services.match_scorer import score_match

from verifiers._http import (
    UpstreamError,
    build_headers,
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
)

TRDIZIN_API = "https://search.trdizin.gov.tr/api/defaultSearch/publication/"
_HOST = "search.trdizin.gov.tr"


async def search(source: ParsedSource) -> MatchResult | None:
    """Search TRDizin by title via their JSON API."""
    query = source.title
    if not query:
        return None

    params = {
        "q": query,
        "order": "relevance-DESC",
        "page": "1",
        "limit": "5",
    }

    search_url = f"https://search.trdizin.gov.tr/tr/yayin/ara?q={quote(query, safe=',')}&order=relevance-DESC&page=1&limit=20"

    session = get_session()
    check_parked_url(TRDIZIN_API)
    async with session.get(TRDIZIN_API, params=params, headers=build_headers()) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        # search.trdizin.gov.tr intermittently returns HTML error pages or
        # text/plain payloads on transient backend hiccups. Surface those
        # as ``error`` (red dot with detail) so users can tell an upstream
        # blip from a real "not in TRDizin" miss — matches the policy in
        # the rest of the verifiers since the JSON-error promotion.
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as e:
            raise UpstreamError(_HOST, 200, f"invalid JSON: {e}") from e

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None

        best: MatchResult | None = None
        for hit in hits[:5]:
            match = _hit_to_match(hit, source, search_url)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _hit_to_match(hit: dict[str, Any], source: ParsedSource, search_url: str) -> MatchResult | None:
    """Convert a TRDizin API hit to a MatchResult."""
    src = hit.get("_source", {})
    if not src:
        return None

    # Title: from abstracts array (prefer Turkish or first available)
    title = ""
    abstracts = src.get("abstracts", [])
    for abstract in abstracts:
        t = abstract.get("title", "")
        if t:
            title = t
            break

    if not title:
        return None

    # Authors
    authors = []
    for author in src.get("authors", []):
        name = author.get("name", "")
        if name:
            authors.append(name)

    # Year
    year = src.get("publicationYear")

    # DOI
    doi = src.get("doi", "") or ""

    # Journal
    journal = ""
    journal_info = src.get("journal", {})
    if journal_info:
        journal = journal_info.get("name", "")

    # URL: construct from the record ID
    record_id = hit.get("_id", "")
    url = f"https://search.trdizin.gov.tr/tr/yayin/detay/{record_id}" if record_id else ""
    if doi:
        url = f"https://doi.org/{doi}"

    # Pages: prefer "page" if present, otherwise build from start/end.
    pages: str | None = src.get("page") or None
    if not pages:
        sp = src.get("startPage") or ""
        ep = src.get("endPage") or ""
        if sp and ep:
            pages = f"{sp}-{ep}"
        else:
            pages = sp or ep or None

    # Language tokens are short codes like "TUR" / "ENG"; normalise lowercase.
    lang_raw = src.get("lang") or src.get("language") or ""
    language = str(lang_raw).strip().lower() if lang_raw else ""

    # TRDizin nests bibliographic detail inside `issue` (an object, not a
    # string): {"number": "2", "volume": "6", "year": "2023", ...}. The
    # top-level `volume` field is always null — the real volume lives at
    # issue.volume. Pull both from the nested object; fall back to the
    # top-level keys only if the schema ever flattens them.
    raw_issue = src.get("issue")
    issue_obj = raw_issue if isinstance(raw_issue, dict) else {}
    volume_raw = issue_obj.get("volume") or src.get("volume")
    issue_raw = issue_obj.get("number") or (raw_issue if not isinstance(raw_issue, dict) else None)
    volume = str(volume_raw).strip() if volume_raw not in (None, "") else None
    issue = str(issue_raw).strip() if issue_raw not in (None, "") else None

    candidate = {
        "database": "TRDizin",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": search_url,
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "language": language,
    }

    return score_match(source, candidate)
