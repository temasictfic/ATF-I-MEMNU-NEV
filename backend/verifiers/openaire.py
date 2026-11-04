"""OpenAIRE Graph API v2 verifier — European open-science aggregator.

Covers research products from OpenAIRE's harvested network of repositories,
open-access journals, and data sources (OpenAlex-like coverage with a European
and green-OA bias). No API key required.
"""

import time
from typing import Any
from urllib.parse import quote

import aiohttp
from models.source import ParsedSource
from models.verification_result import MatchResult
from scrapers.rate_limiter import rate_limiter
from services.match_scorer import score_match
from services.openaire_token_manager import get_access_token
from services.scoring_constants import DOI_MATCH_MIN_SCORE

from verifiers._http import (
    RateLimitedError,
    UnauthorizedError,
    check_parked_url,
    check_rate_limit,
    fetch_with_year_fallback,
    get_session,
    raise_for_unexpected_status,
    strip_lucene_special,
)

OPENAIRE_API = "https://api.openaire.eu/graph/v2/researchProducts"
_HOST = "api.openaire.eu"

# Authenticated OpenAIRE users get 7,200 req/hour — a safe per-request
# cadence of 0.5 s (2 req/s) leaves ample headroom. Anonymous users cap
# at 60 req/hour, which simple inter-request pacing can't enforce; the
# sliding-window quota below is the actual enforcement mechanism.
_AUTHENTICATED_PACE_SECONDS = 0.5

# Hour-window caps (sliding). Updated live when the user adds, refreshes,
# or removes a refresh token via openaire_token_manager. The window is
# preventive — we refuse to send the 61st (anon) / 7,201st (authed)
# request rather than relying on reactive 429-park.
_ANON_HOURLY_CAP = 60
_AUTHED_HOURLY_CAP = 7200
_HOUR_SECONDS = 3600

# Register the safe (anonymous) cap at import time. The token manager
# upgrades to the authed cap whenever a refresh token is configured.
rate_limiter.register_window(_HOST, _ANON_HOURLY_CAP, _HOUR_SECONDS)

# Cooldown after OpenAIRE rejects an attached Bearer — usually a stale or
# revoked access token. Refresh-token exchange failures are already
# surfaced through openaire_token_manager._runtime_status, so this only
# fires for the in-flight authenticated request path.
_UNAUTHORIZED_COOLDOWN_SEC = 3600.0
_unauthorized_until: float = 0.0
_UNAUTHORIZED_HINT = (
    "OpenAIRE rejected the access token. Reconnect your OpenAIRE refresh "
    "token in Settings → API Keys."
)


def update_window_for_auth_state(*, authenticated: bool) -> None:
    """Swap the OpenAIRE hour-window cap to match the current auth state.

    Called by openaire_token_manager whenever the refresh token is added,
    rotated, or cleared. Re-registering replaces the deque (i.e. clears
    the timestamp history) so a previously full anon-bucket can't strand
    a freshly authed user behind 60 dead timestamps.
    """
    cap = _AUTHED_HOURLY_CAP if authenticated else _ANON_HOURLY_CAP
    rate_limiter.register_window(_HOST, cap, _HOUR_SECONDS)


def _openaire_pace_seconds() -> float | None:
    """Return a per-request pace override when the user is authenticated.

    Mirrors the Crossref polite-pool pattern: when a refresh token is
    configured in settings, the verifier's request will carry a Bearer
    header and qualify for the authenticated rate tier, so we can cadence
    almost 2× faster than the limiter's anonymous-safe default.
    """
    try:
        from services.settings_store import get_current_settings
        if (get_current_settings().api_keys.get("openaire", "") or "").strip():
            return _AUTHENTICATED_PACE_SECONDS
    except Exception:  # noqa: BLE001, S110
        pass
    return None


async def search(source: ParsedSource) -> MatchResult | None:
    """Search OpenAIRE by DOI first, then by title."""
    session = get_session()

    # Priority 1: DOI via persistent identifier filter (pid covers DOI, PMID, etc.)
    if source.doi:
        result = await _fetch_best_match(session, {"pid": source.doi, "pageSize": "5"}, source)
        if result and result.score >= DOI_MATCH_MIN_SCORE:
            return result

    # Priority 2: Title search. `mainTitle` is the structured title filter on
    # v2 — Lucene-backed, so a stray ``(`` (e.g. titles ending with "...
    # (DCNN" with a missing close paren) is a hard parse error and surfaces
    # as 400. Strip the Lucene special chars before sending.
    query = strip_lucene_special(source.title or "")
    if not query:
        return None

    params: dict[str, str] = {
        "mainTitle": query,
        "type": "publication",
        "pageSize": "5",
    }

    # Year range ±1 disambiguates editions without blocking the common
    # print-vs-online date drift.
    if source.year:
        params["fromPublicationDate"] = f"{source.year - 1}-01-01"
        params["toPublicationDate"] = f"{source.year + 1}-12-31"

    return await fetch_with_year_fallback(
        lambda p: _fetch_best_match(session, p, source),
        params,
        {"fromPublicationDate", "toPublicationDate"},
    )


async def _fetch_best_match(
    session: aiohttp.ClientSession,
    params: dict[str, str],
    source: ParsedSource,
) -> MatchResult | None:
    """Execute one OpenAIRE request and return the highest-scoring candidate.

    When the user has connected a personal refresh token, we attach the
    exchanged access token to lift the per-IP rate limit from 60 req/hour
    (anonymous) to 7,200 req/hour. A missing or broken token silently falls
    back to anonymous so a bad paste never breaks verification.
    """
    global _unauthorized_until
    now = time.monotonic()

    check_parked_url(OPENAIRE_API)
    if not rate_limiter.consume_window(_HOST):
        wait = rate_limiter.window_seconds_until_slot(_HOST)
        # Park the host so sibling sources fail-fast via check_parked_url
        # for the rest of the window — no point letting them waste task
        # slots queueing for pacing only to be refused by the window.
        rate_limiter.park(_HOST, wait)
        raise RateLimitedError(_HOST, retry_after=wait, status=429)
    await rate_limiter.acquire(_HOST, rate=_openaire_pace_seconds())
    access_token = await get_access_token()
    headers = (
        {"Authorization": f"Bearer {access_token}"} if access_token else None
    )
    has_bearer = access_token is not None
    if has_bearer and now < _unauthorized_until:
        raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT)
    async with session.get(OPENAIRE_API, params=params, headers=headers) as resp:
        check_rate_limit(resp)
        if has_bearer and resp.status in (401, 403):
            _unauthorized_until = now + _UNAUTHORIZED_COOLDOWN_SEC
            raise UnauthorizedError(_HOST, detail=_UNAUTHORIZED_HINT, status=resp.status)
        raise_for_unexpected_status(_HOST, resp)
        if resp.status != 200:
            return None
        data = await resp.json()
        results = data.get("results", []) or []

        best: MatchResult | None = None
        for item in results[:5]:
            match = _item_to_match(item, source)
            if match and (best is None or match.score > best.score):
                best = match
        return best


def _item_to_match(item: dict[str, Any], source: ParsedSource) -> MatchResult | None:
    title = item.get("mainTitle", "") or ""

    # authors: list of {fullName, name, surname, rank, pid?}
    authors: list[str] = []
    for author in item.get("authors", []) or []:
        if not isinstance(author, dict):
            continue
        name = author.get("fullName") or author.get("name") or author.get("surname") or ""
        if name:
            authors.append(name)

    # publicationDate: "YYYY-MM-DD"
    year = None
    pub_date = item.get("publicationDate") or ""
    if pub_date:
        try:
            year = int(str(pub_date)[:4])
        except (ValueError, TypeError):
            pass

    # DOI lives in pids as [{scheme: "doi", value: ...}]
    doi = ""
    for pid in item.get("pids", []) or []:
        if not isinstance(pid, dict):
            continue
        if (pid.get("scheme") or "").lower() == "doi" and pid.get("value"):
            doi = pid["value"]
            break

    # container: {name, issnPrinted, issnOnline, volume, issue, sp, ep}
    container = item.get("container") or {}
    journal = container.get("name", "") if isinstance(container, dict) else ""

    # Prefer DOI URL, then the first instance URL.
    url = f"https://doi.org/{doi}" if doi else ""
    if not url:
        for instance in item.get("instances", []) or []:
            urls = instance.get("urls") if isinstance(instance, dict) else None
            if isinstance(urls, list) and urls:
                url = urls[0]
                break

    # Bibliographic extras from container.{vol, iss, sp, ep, issnPrinted, issnOnline}
    volume = (container.get("vol") if isinstance(container, dict) else None) or None
    issue = (container.get("iss") if isinstance(container, dict) else None) or None
    sp = container.get("sp") if isinstance(container, dict) else ""
    ep = container.get("ep") if isinstance(container, dict) else ""
    if sp and ep:
        pages = f"{sp}-{ep}"
    else:
        pages = sp or ep or None

    issn_list: list[str] = []
    for k in ("issnPrinted", "issnOnline"):
        v = container.get(k) if isinstance(container, dict) else ""
        if v and v not in issn_list:
            issn_list.append(v)

    # language can be a {code, label} dict or a plain string.
    lang_raw = item.get("language") or ""
    if isinstance(lang_raw, dict):
        language = lang_raw.get("code", "") or ""
    else:
        language = str(lang_raw) or ""

    # Document type — prefer the formal instancetype name, fall back to subtypes.
    document_type = ""
    instances = item.get("instances") or []
    if isinstance(instances, list) and instances:
        first = instances[0] if isinstance(instances[0], dict) else {}
        instype = first.get("instancetype") if isinstance(first, dict) else None
        if isinstance(instype, dict):
            document_type = instype.get("name", "") or ""

    search_query = source.title or (source.raw_text[:100] if source.raw_text else "")
    candidate = {
        "database": "OpenAIRE",
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "journal": journal,
        "url": url,
        "search_url": f"https://explore.openaire.eu/search/find?fv0={quote(search_query)}&f0=q",
        "volume": volume,
        "issue": issue,
        "pages": pages,
        "language": language,
        "document_type": document_type,
        "issn": issn_list,
    }

    return score_match(source, candidate)
