"""arXiv API verifier - search via Atom feed API.

Only runs when the source already contains an arXiv URL or ID. The previous
title-fallback path was removed: arXiv's Lucene query is fragile on cleaned
non-English titles, and across humanities / Turkish references it is almost
always a no-op anyway. Issuing a request per source piled them up behind the
3 s pacing window and surfaced as ``timeout`` dots once the queue depth
crossed the search-timeout budget. Now arXiv only fires when there is a
genuine arXiv identifier to look up — which is precise, fast, and rate-safe.
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from services.match_scorer import score_match
from utils.doi_extractor import extract_arxiv_id

from verifiers._http import (
    UpstreamError,
    acquire_or_rate_limited,
    build_headers,
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
)

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_HOST = "export.arxiv.org"
NS = {"atom": "http://www.w3.org/2005/Atom"}

# Cap on how long the rate-limiter is allowed to make us wait. Fail fast as
# ``rate_limited`` past this — the per-DB search timeout is typically 20 s
# and we don't want the queue alone to consume the entire budget.
_PACING_MAX_WAIT_SECONDS = 8.0


def _strip_arxiv_version(url: str) -> str:
    """Remove the version suffix (v1, v2, …) from an arXiv abstract URL.

    https://arxiv.org/abs/2010.11929v2  →  https://arxiv.org/abs/2010.11929
    http://arxiv.org/abs/2010.11929v12  →  http://arxiv.org/abs/2010.11929
    """
    return re.sub(r"v\d+$", "", url)


async def search(source: ParsedSource) -> MatchResult | None:
    """Search arXiv by ID only — no title fallback.

    Extracts an arXiv identifier from the source URL or raw text and
    issues an ``id_list`` lookup, which is precise and produces a 1.0
    score via the arXiv-ID branch of ``_url_match_score``. When no ID
    is present we return ``None`` immediately, which the orchestrator
    paints as a ``no_match`` dot — informative, and crucially without
    eating a slot in the 3 s-paced rate-limiter queue.
    """
    arxiv_id = extract_arxiv_id(source.url or "") or extract_arxiv_id(
        source.raw_text
    )
    if not arxiv_id:
        return None

    # Strip any version suffix so the API returns the canonical record;
    # the scored URL will also be version-free.
    base_id = re.sub(r"v\d+$", "", arxiv_id)

    # Pacing happens here, only on the path that actually issues a
    # request, and bounded so a deep queue surfaces as ``rate_limited``
    # instead of swallowing the search timeout.
    await acquire_or_rate_limited(ARXIV_HOST, _PACING_MAX_WAIT_SECONDS)

    session = get_session()
    return await _lookup_by_id(session, base_id, source)


async def _lookup_by_id(
    session: aiohttp.ClientSession,
    arxiv_id: str,
    source: ParsedSource,
) -> MatchResult | None:
    """Fetch a single arXiv paper by its ID using the id_list parameter."""
    params = {"id_list": arxiv_id}
    check_parked_url(ARXIV_API)
    async with session.get(ARXIV_API, params=params, headers=build_headers()) as resp:
        check_rate_limit(resp)
        raise_for_unexpected_status(ARXIV_HOST, resp)
        if resp.status != 200:
            return None
        text = await resp.text()
        return _parse_atom_response(text, source)


def _parse_atom_response(xml_text: str, source: ParsedSource) -> MatchResult | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise UpstreamError(ARXIV_HOST, 200, f"invalid Atom XML: {e}") from e

    entries = root.findall("atom:entry", NS)
    best: MatchResult | None = None

    search_query = source.title or (source.raw_text[:100] if source.raw_text else "")

    for entry in entries[:5]:
        title_el = entry.find("atom:title", NS)
        title = (
            (title_el.text or "").strip().replace("\n", " ")
            if title_el is not None
            else ""
        )

        # arXiv signals errors (e.g. malformed id) as a 200-OK feed with a
        # single entry whose title is "Error" and id under /api/errors.
        # See arXiv user manual section "Errors". Skip those so they don't
        # get scored as a low-quality match.
        id_el = entry.find("atom:id", NS)
        entry_id = (id_el.text or "") if id_el is not None else ""
        if title == "Error" or "/api/errors" in entry_id:
            continue

        authors = []
        for author in entry.findall("atom:author", NS):
            name_el = author.find("atom:name", NS)
            if name_el is not None and name_el.text:
                authors.append(name_el.text.strip())

        published = entry.find("atom:published", NS)
        year = None
        if published is not None and published.text:
            try:
                year = int(published.text[:4])
            except ValueError:
                pass

        # Prefer the text/html link (abstract page); fall back to atom:id.
        link = ""
        for link_el in entry.findall("atom:link", NS):
            if link_el.get("type") == "text/html":
                link = link_el.get("href", "")
                break
        if not link:
            link = entry_id

        # Strip version suffix so URL comparison in _url_match_score works
        # regardless of whether the source citation includes a version or not.
        # e.g. https://arxiv.org/abs/2010.11929v2 → https://arxiv.org/abs/2010.11929
        link = _strip_arxiv_version(link)

        candidate: dict[str, object] = {
            "database": "arXiv",
            "title": title,
            "authors": authors,
            "year": year,
            "doi": None,
            "journal": "arXiv",
            "url": link,
            "search_url": f"https://arxiv.org/search/?query={quote(search_query)}&searchtype=all",
            "document_type": "preprint",
            "language": "en",
        }

        match = score_match(source, candidate)
        if match and (best is None or match.score > best.score):
            best = match

    return best
