"""Europe PMC API verifier - free biomedical and life sciences literature."""

from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from services.match_scorer import score_match
from services.scoring_constants import DOI_MATCH_MIN_SCORE

from verifiers._http import (
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
    strip_phrase_chars,
)

EUROPE_PMC_API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_HOST = "www.ebi.ac.uk"


async def search(source: ParsedSource) -> MatchResult | None:
    """Search Europe PMC by title (or DOI if available)."""
    session = get_session()
    # Priority 1: DOI lookup
    if source.doi:
        result = await _search_query(session, f'DOI:"{source.doi}"', source)
        if result and result.score >= DOI_MATCH_MIN_SCORE:
            return result

    # Priority 2: Title search. Sanitise the title before wrapping in a
    # quoted Lucene phrase — embedded ``"`` or ``\`` would unbalance it
    # and surface as a 400 from Europe PMC.
    query = strip_phrase_chars(source.title or "")
    if not query:
        return None

    return await _search_query(session, f'TITLE:"{query}"', source)


async def _search_query(
    session: aiohttp.ClientSession, query: str, source: ParsedSource
) -> MatchResult | None:
    """Execute a Europe PMC search and return the best match."""
    params = {
        "query": query,
        "format": "json",
        "pageSize": "5",
    }
    check_parked_url(EUROPE_PMC_API)
    async with session.get(EUROPE_PMC_API, params=params) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        results = data.get("resultList", {}).get("result", [])

        best: MatchResult | None = None
        for item in results[:5]:
            match = _item_to_match(item, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _item_to_match(item: dict, source: ParsedSource) -> MatchResult | None:
    title = item.get("title", "")

    # Authors come as a single string: "Author A, Author B, Author C"
    author_string = item.get("authorString", "")
    authors = [a.strip() for a in author_string.split(",") if a.strip()] if author_string else []

    year = None
    pub_year = item.get("pubYear")
    if pub_year:
        try:
            year = int(pub_year)
        except (ValueError, TypeError):
            pass

    doi = item.get("doi", "")
    journal = item.get("journalTitle", "")

    # Build article URL
    source_type = item.get("source", "")
    ext_id = item.get("id", "")
    if source_type and ext_id:
        url = f"https://europepmc.org/article/{source_type}/{ext_id}"
    else:
        url = ""

    issn_list = [
        v for v in (item.get("journalIssn"), item.get("essn"))
        if isinstance(v, str) and v
    ]

    search_query = source.title or source.raw_text[:100]
    candidate = {
        "database": "Europe PMC",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": f"https://europepmc.org/search?query={quote(search_query)}",
        "volume": item.get("journalVolume") or None,
        "issue": item.get("issue") or None,
        "pages": item.get("pageInfo") or None,
        "document_type": item.get("pubType", "") or item.get("docType", "") or "",
        "language": item.get("language", "") or "",
        "issn": issn_list,
    }

    return score_match(source, candidate)
