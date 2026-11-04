"""Web of Science Starter API verifier.

Clarivate's WoS Starter API uses an ``X-ApiKey`` header for authentication.
Without an API key (read from ``api_keys["wos"]`` in settings) the verifier
returns ``None`` silently — same opt-in pattern as BASE — so a user who
toggles WoS on without entering a key still sees a clean "no_match" rather
than a spammy red error dot.

Endpoint works with both free Starter tokens and Expanded API keys issued
under an institutional Web of Science Core Collection subscription. Apply
at https://developer.clarivate.com/.
"""

import time
from typing import Any
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from scrapers.rate_limiter import rate_limiter
from services.daily_quota import daily_quota
from services.match_scorer import score_match
from services.scoring_constants import DOI_MATCH_MIN_SCORE
from services.settings_store import get_current_settings

from verifiers._http import (
    RateLimitedError,
    UnauthorizedError,
    UpstreamError,
    build_headers,
    check_parked_url,
    check_rate_limit,
    get_session,
    raise_for_unexpected_status,
    strip_phrase_chars,
)

# Clarivate's OpenAPI spec lists both /v1 and /v2 as supported servers with
# identical paths and auth. v2's response shape is a superset of v1 (adds
# names.contributors[] sourced from ORCID, which we don't currently parse
# but could later); every field score_match consumes — title, names.authors,
# source.sourceTitle / publishYear / volume / issue / pages, identifiers.doi,
# links.record, types — is unchanged. Use v2 since it's the actively
# extended surface; v1 is still live but receives no new fields.
WOS_API = "https://api.clarivate.com/apis/wos-starter/v2/documents"
_HOST = "api.clarivate.com"

# Once Clarivate rejects this process for an invalid/expired key, suppress
# further outbound requests for an hour so we don't keep burning the daily
# quota and don't get the key flagged. UnauthorizedError still surfaces per
# source so the UI stays consistent.
_UNAUTHORIZED_COOLDOWN_SEC = 3600.0
_unauthorized_until: float = 0.0
_UNAUTHORIZED_HINT = (
    "Invalid or expired Clarivate API key. Update it in Settings → API Keys."
)

# Daily request caps for the two user-selectable Web of Science *Starter*
# API tiers. The Clarivate signup form at developer.clarivate.com only
# offers Free Trial and Institutional Member; the Free Institutional
# Integration plan (20,000/day) exists in the docs but is a custom
# contract arrangement that no end user picks during normal registration,
# so it's intentionally omitted here.
#
# The verifier only hits the Starter endpoint — the WoS API Expanded
# product (separate endpoint, separate key, multi-tier annual/monthly
# pricing) is not represented here. Free is the default because a user
# who pasted a key without telling us which tier it is is safer treated
# as the strictest cap: over-spending on the free trial just slows us
# down for the rest of the day, but over-spending on a tier we don't
# actually have can get the key revoked.
_DAILY_CAPS: dict[str, int] = {
    # "Free Trial Plan" — open-registration developer key, 1 req/s.
    "starter_free": 50,
    # "Institutional Plan" — granted to users at WoS-subscribing
    # institutions, 5 req/s.
    "starter_institutional": 5000,
}
_DEFAULT_TIER = "starter_free"


def _wos_tier() -> str:
    """Read the user-selected WoS tier from settings, defaulting to the free cap."""
    try:
        tier = (get_current_settings().api_keys.get("wos_tier") or "").strip()
    except Exception:  # noqa: BLE001
        tier = ""
    return tier if tier in _DAILY_CAPS else _DEFAULT_TIER


def _wos_pace_seconds() -> float | None:
    """Per-request pacing override matching the user's WoS tier ceiling.

    Free Trial is 1 req/s — the limiter's default for an unconfigured host
    is already conservative enough, so return ``None`` to let it govern.
    Institutional Member is 5 req/s — return 0.21 s pace (≈ 4.76 req/s)
    to leave a small headroom for clock drift before tripping a 429.
    """
    if _wos_tier() == "starter_institutional":
        return 0.21
    return None


async def _read_wos_error_detail(resp: aiohttp.ClientResponse) -> str:
    """Pull the most specific human-readable message out of a WoS error body.

    Per the v2 OpenAPI spec:
      - 400 / 404 / 405 → ``{"error": {"status", "title", "details"}}``
      - 401            → ``{"error": "invalid_request", "error_description": "..."}``

    Returns ``""`` if the body is unparseable or the shape is unexpected,
    so callers can fall back to a generic message.
    """
    try:
        data = await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("details") or err.get("title") or "").strip()
    desc = data.get("error_description")
    if isinstance(desc, str):
        return desc.strip()
    return ""


async def search(source: ParsedSource, api_key: str | None = None) -> MatchResult | None:
    """Search WoS Core Collection by DOI then by title.

    DOI lookup is preferred — WoS query syntax ``DO=<doi>`` returns the
    canonical record directly, no scoring noise. Falls back to ``TI=`` title
    search with a ±1-year filter when no DOI is parsed.
    """
    if not api_key:
        return None

    session = get_session()
    headers = {**build_headers(), "X-ApiKey": api_key}

    if source.doi:
        params = {"q": f"DO={source.doi}", "db": "WOS", "limit": "5"}
        result = await _query(session, params, source, headers)
        if result and result.score >= DOI_MATCH_MIN_SCORE:
            return result

    title = (source.title or "").strip()
    if not title:
        return None

    # WoS query: TI for the title field, wrapped in a quoted phrase. Strip
    # ``"`` and ``\`` — the only chars that can break the phrase syntax —
    # and collapse whitespace via the shared sanitiser.
    safe_title = strip_phrase_chars(title)
    if not safe_title:
        return None
    base_q = f'TI="{safe_title}"'

    base_params: dict[str, str] = {"q": base_q, "db": "WOS", "limit": "5"}

    # Year filtering uses the documented ``publishTimeSpan`` parameter, not
    # ``PY=YYYY-YYYY`` inside ``q``. The v2 OpenAPI spec only documents
    # exact-year ``PY=`` matches in queries; ranges go through the dedicated
    # parameter (format ``yyyy-mm-dd yyyy-mm-dd``, the ``+`` separator in
    # the docs is just the URL-encoded form of the space).
    if source.year:
        params_with_year = {
            **base_params,
            "publishTimeSpan": (
                f"{source.year - 1}-01-01 {source.year + 1}-12-31"
            ),
        }
        result = await _query(session, params_with_year, source, headers)
        if result is not None:
            return result

    return await _query(session, base_params, source, headers)


async def _query(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
    headers: dict[str, str],
) -> MatchResult | None:
    """Execute one WoS request and return the highest-scoring hit."""
    global _unauthorized_until
    now = time.monotonic()
    if now < _unauthorized_until:
        raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT)

    check_parked_url(WOS_API)
    tier = _wos_tier()
    cap = _DAILY_CAPS[tier]
    if not daily_quota.consume(f"wos:{tier}", cap):
        # Daily cap exhausted — park siblings until close to UTC midnight
        # so the rest of this PDF (and later PDFs in a batch) fail-fast
        # without re-checking the quota each time. Cap the park at 1 hour
        # to keep the limiter's worst-case stall bounded.
        reset = daily_quota.seconds_until_reset()
        rate_limiter.park(_HOST, min(reset, 3600.0))
        raise RateLimitedError(_HOST, retry_after=reset, status=429)
    await rate_limiter.acquire(_HOST, rate=_wos_pace_seconds())
    async with session.get(WOS_API, params=params, headers=headers) as resp:
        check_rate_limit(resp)
        if resp.status in (401, 403):
            _unauthorized_until = now + _UNAUTHORIZED_COOLDOWN_SEC
            wos_detail = await _read_wos_error_detail(resp)
            raise UnauthorizedError(
                _HOST,
                detail=wos_detail or _UNAUTHORIZED_HINT,
                status=resp.status,
            )
        if resp.status == 400:
            # Spec sends ``{error: {status, title, details}}`` — the ``details``
            # field is the actually useful one ("invalid query syntax near
            # token X"). Surface it instead of the generic "bad request".
            wos_detail = await _read_wos_error_detail(resp)
            raise UpstreamError(_HOST, 400, wos_detail or "bad request")
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        try:
            data = await resp.json(content_type=None)
        except (aiohttp.ContentTypeError, ValueError) as e:
            raise UpstreamError(_HOST, 200, f"invalid JSON: {e}") from e

        hits = data.get("hits") or []
        if not isinstance(hits, list):
            return None

        best: MatchResult | None = None
        for hit in hits[:5]:
            match = _hit_to_match(hit, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _hit_to_match(hit: dict[str, Any], source: ParsedSource) -> MatchResult | None:
    """Convert a WoS Starter API hit to a MatchResult."""
    title = hit.get("title") or ""
    if not title:
        return None

    names = hit.get("names") or {}
    authors_raw = names.get("authors") if isinstance(names, dict) else []
    authors: list[str] = []
    if isinstance(authors_raw, list):
        for a in authors_raw:
            if isinstance(a, dict):
                name = a.get("displayName") or a.get("wosStandard") or ""
                if name:
                    authors.append(name)

    src_obj = hit.get("source") or {}
    if not isinstance(src_obj, dict):
        src_obj = {}
    journal = src_obj.get("sourceTitle") or ""

    year_raw = src_obj.get("publishYear")
    year: int | None = None
    if year_raw not in (None, ""):
        try:
            year = int(year_raw)
        except (ValueError, TypeError):
            year = None

    volume_raw = src_obj.get("volume")
    issue_raw = src_obj.get("issue")
    volume = str(volume_raw).strip() if volume_raw not in (None, "") else None
    issue = str(issue_raw).strip() if issue_raw not in (None, "") else None

    # WoS Starter encodes pages as {"range": "1-10", "begin": "1", "end": "10"}
    # but older / partial records may carry just a string. Accept both.
    pages: str | None = None
    pages_obj = src_obj.get("pages")
    if isinstance(pages_obj, dict):
        pages = pages_obj.get("range") or None
        if not pages:
            begin, end = pages_obj.get("begin"), pages_obj.get("end")
            if begin and end:
                pages = f"{begin}-{end}"
            elif begin:
                pages = str(begin)
    elif isinstance(pages_obj, str) and pages_obj:
        pages = pages_obj

    identifiers = hit.get("identifiers") or {}
    if not isinstance(identifiers, dict):
        identifiers = {}
    doi = identifiers.get("doi") or ""
    issn_raw = identifiers.get("issn") or identifiers.get("eissn") or ""
    issn_list = [issn_raw] if issn_raw else []

    links = hit.get("links") or {}
    record_url = links.get("record") if isinstance(links, dict) else ""
    url = f"https://doi.org/{doi}" if doi else (record_url or "")

    types = hit.get("types") or []
    document_type = ""
    if isinstance(types, list) and types:
        document_type = str(types[0]) if types[0] else ""

    search_query = source.title or (source.raw_text[:100] if source.raw_text else "")
    candidate = {
        "database": "Web of Science",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": f"https://www.webofscience.com/wos/woscc/basic-search?type=general&value={quote(search_query)}",
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "document_type": document_type,
        "issn": issn_list,
    }
    return score_match(source, candidate)
