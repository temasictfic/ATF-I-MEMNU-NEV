"""Open Library API verifier - Internet Archive's open book catalog.

Open Library covers 20M+ edition records with strong book/monograph coverage.
No API key required.

Open Library indexes books, not articles, so its search index does not
contain DOIs. We don't try a DOI lookup — it would burn a request on a
field the schema doesn't expose. Article-DOI sources should match against
Crossref / OpenAlex / Europe PMC instead.
"""

from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from services.match_scorer import score_match

from verifiers._http import (
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
)

SEARCH_API = "https://openlibrary.org/search.json"
_HOST = "openlibrary.org"


async def search(source: ParsedSource) -> MatchResult | None:
    """Search Open Library by title.

    Errors propagate to the orchestrator so they're surfaced as
    ``db_status: "error"`` rather than silently ``not_found``.
    """
    query = source.title
    if not query:
        return None

    session = get_session()
    return await _search_query(session, {"title": query}, source)


async def _search_query(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
) -> MatchResult | None:
    """Execute an Open Library search and return the best match."""
    params = {**params, "limit": "5", "fields": "title,author_name,first_publish_year,isbn,key,publisher,subject"}

    check_parked_url(SEARCH_API)
    async with session.get(SEARCH_API, params=params) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        docs = data.get("docs", [])

        best: MatchResult | None = None
        for item in docs[:5]:
            match = _item_to_match(item, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _item_to_match(item: dict, source: ParsedSource) -> MatchResult | None:
    title = item.get("title", "")

    authors = item.get("author_name", [])
    if not isinstance(authors, list):
        authors = []

    year = None
    pub_year = item.get("first_publish_year")
    if pub_year:
        try:
            year = int(pub_year)
        except (ValueError, TypeError):
            pass

    # Open Library doesn't store DOIs directly
    doi = ""

    # Build URL from the work key
    key = item.get("key", "")
    url = f"https://openlibrary.org{key}" if key else ""

    publishers = item.get("publisher", [])
    if not isinstance(publishers, list):
        publishers = []
    publisher = publishers[0] if publishers else ""
    # journal is a no-op for books; keep field present so scoring sees ""
    journal = ""

    raw_isbns = item.get("isbn", []) or []
    isbn_list = [s for s in raw_isbns if isinstance(s, str) and s]

    search_query = source.title or source.raw_text[:100]
    candidate = {
        "database": "Open Library",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": f"https://openlibrary.org/search?q={quote(search_query)}",
        "publisher": publisher,
        "document_type": "book",
        "isbn": isbn_list,
    }

    return score_match(source, candidate)
