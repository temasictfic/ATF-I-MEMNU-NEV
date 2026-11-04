"""Shared aiohttp session for verifier HTTP calls.

Verifiers historically opened a fresh `aiohttp.ClientSession` on every call,
which defeats TCP connection pooling and repeats the TLS handshake for every
citation. This module exposes a single lazily-created session that is reused
across calls, and a shutdown hook the FastAPI app can wire into lifespan.
"""

import asyncio
import ssl
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import TypeVar
from urllib.parse import urlparse

import aiohttp
from scrapers.rate_limiter import rate_limiter
from services.search_settings import get_client_timeout, get_polite_pool_email

T = TypeVar("T")

_session: aiohttp.ClientSession | None = None


def create_ssl_context() -> ssl.SSLContext:
    """Build an SSL context that trusts both OS and certifi CA certificates.

    Corporate proxies with TLS inspection inject their own root CA into the
    Windows certificate store.  The bundled certifi bundle doesn't include
    those, so ``aiohttp`` raises ``SSLCertVerificationError``.  Loading the
    OS store *first* (via ``ssl.create_default_context()``) and then layering
    the certifi bundle on top covers both vanilla installs and corporate
    environments without weakening security.
    """
    ctx = ssl.create_default_context()  # loads OS CA store
    try:
        import certifi

        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        pass
    return ctx


class RateLimitedError(Exception):
    """Raised when an upstream API returns HTTP 429.

    Carries the host and the server-advertised retry-after (in seconds)
    so the orchestrator can surface a distinct UI state and the rate
    limiter can park the host until the window elapses.
    """

    def __init__(self, host: str, retry_after: float | None = None, status: int = 429):
        self.host = host
        self.retry_after = retry_after
        self.status = status
        msg = f"{host} returned {status}"
        if retry_after is not None:
            msg += f" (retry-after {retry_after:.0f}s)"
        super().__init__(msg)


class UnauthorizedError(Exception):
    """Raised when an upstream API rejects the caller's credentials or IP.

    Distinct from RateLimitedError because the remediation is different —
    register an allowlist entry / fix the API key, not back off and retry.
    BASE in particular returns HTTP 200 with a JSON ``error`` body for IP
    denials, so verifiers may need to inspect the response body, not just
    the status code, before raising this.
    """

    def __init__(self, host: str, detail: str = "", status: int | None = None):
        self.host = host
        self.detail = detail
        self.status = status
        msg = f"{host} unauthorized"
        if status is not None:
            msg += f" (HTTP {status})"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class UpstreamError(Exception):
    """Raised when an upstream API returns a 5xx, an unexpected 4xx, or a
    malformed body. Falls through to the orchestrator's generic ``except
    Exception`` clause and surfaces as ``db_status: "error"`` — distinct
    from a real ``no_match`` so users can tell "DB had a hiccup" apart
    from "DB doesn't have this paper".

    401/403/404/429 are intentionally NOT raised through this — they have
    their own typed exceptions or legitimate ``no_match`` semantics.
    """

    def __init__(self, host: str, status: int | None, detail: str = ""):
        self.host = host
        self.status = status
        self.detail = detail
        msg = f"{host} returned"
        if status is not None:
            msg += f" HTTP {status}"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


def raise_for_unexpected_status(host: str, resp: aiohttp.ClientResponse) -> None:
    """Raise ``UpstreamError`` for 5xx and unexpected 4xx responses.

    Skips 401/403 (handled by per-verifier ``UnauthorizedError`` raises),
    404 (legitimate "not found" → silent ``no_match``), and 429 (already
    raised as ``RateLimitedError`` by ``check_rate_limit`` upstream).
    Call this immediately after ``check_rate_limit`` and any auth-status
    check, before the verifier's ``if resp.status != 200: return None``
    fallthrough — only 404 should reach the silent path now.
    """
    status = resp.status
    if status in (200, 401, 403, 404, 429):
        return
    if 500 <= status < 600:
        raise UpstreamError(host, status, "server error")
    if 400 <= status < 500:
        raise UpstreamError(host, status, "bad request")


def strip_phrase_chars(s: str) -> str:
    """Strip characters that break a quoted-phrase query.

    Used by verifiers (BASE Solr, WoS Clarivate) that interpolate the title
    into ``field:"<title>"``. Inside a quoted phrase, only ``"`` and ``\\``
    are syntactically dangerous — other Solr/WoS operators lose their
    special meaning. Whitespace is collapsed for tidier query strings.
    """
    if not s:
        return ""
    cleaned = s.replace("\\", " ").replace('"', " ")
    return " ".join(cleaned.split())


# Chars that break a *bare* (unquoted) Lucene/CQL query value: grouping that
# must balance, field separator, escape, boost/fuzz operators, phrase delim
# and the boolean ``!``. Chars deliberately left in: ``+ - * ? / & |`` —
# they're either common in legitimate titles ("real-time", "I/O") or are
# only operator-meaningful when prefixing a term, so leaving them avoids
# mangling matches.
_LUCENE_DANGER_CHARS = set('()[]{}^~:\\"!')


def strip_lucene_special(s: str) -> str:
    """Strip Lucene/CQL syntax characters from a bare-query value.

    Used by verifiers (currently OpenAIRE Graph v2) that pass the title
    straight into a Lucene-backed filter without wrapping it in a quoted
    phrase. An unbalanced ``(`` is the most common offender and surfaces
    as HTTP 400 from the upstream parser.
    """
    if not s:
        return ""
    cleaned = "".join(" " if c in _LUCENE_DANGER_CHARS else c for c in s)
    return " ".join(cleaned.split())


def strip_pubmed_field_chars(s: str) -> str:
    """Strip ``[`` and ``]`` so PubMed's ``[Field]`` tag syntax can't be
    hijacked by a title containing brackets.

    PubMed's ESearch builds queries like ``<title>[Title]`` — a title with
    brackets ("Hidden Markov [Models] ...") would surface ``[Models]`` as
    an unknown field tag, returning HTTP 400 or empty results.
    """
    if not s:
        return ""
    return " ".join(s.replace("[", " ").replace("]", " ").split())


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Accepts delta-seconds or HTTP-date."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(timezone.utc)).total_seconds()
    return max(0.0, delta)


def check_parked_url(url: str) -> None:
    """Fail fast if the URL's host is currently parked by the rate limiter.

    Called at the top of each verifier request site. If an earlier 429
    parked this host, we raise ``RateLimitedError`` immediately with the
    remaining cooldown — no network round-trip, no extra 429, no waiting.
    The orchestrator turns that into a ``rate_limited`` UI state and the
    per-DB search for this source ends in milliseconds instead of burning
    the whole ``search_timeout`` asleep inside a rate limiter.
    """
    host = urlparse(url).hostname
    if not host:
        return
    remaining = rate_limiter.parked_remaining(host)
    if remaining > 0:
        raise RateLimitedError(host, retry_after=remaining, status=429)


def check_rate_limit(resp: aiohttp.ClientResponse) -> None:
    """Raise ``RateLimitedError`` if the response is a 429.

    Also parks the response's host in the shared rate limiter so subsequent
    requests back off for the server-advertised window (or a conservative
    default of 60s when no Retry-After header is present).
    """
    if resp.status != 429:
        return
    host = resp.url.host or ""
    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
    # Cap the park window so one rude server can't stall the app forever.
    # Default (no Retry-After header) is 10s — with the new stride-based
    # rotation that caps concurrent per-DB hits at 2 (down from 3), 429s
    # without a server-provided Retry-After are almost always transient
    # contention rather than a sustained quota breach, so a shorter park
    # lets subsequent sources in the same PDF retry sooner. A real quota
    # exhaustion still gets respected because the server will send its
    # own Retry-After value, which we honor verbatim up to the 900s cap.
    park_seconds = min(retry_after if retry_after is not None else 10.0, 900.0)
    if host:
        rate_limiter.park(host, park_seconds)
    raise RateLimitedError(host, retry_after, status=resp.status)


def build_headers() -> dict[str, str]:
    """Return a polite-pool User-Agent header.

    Identifying the client by mailto when configured lets API operators
    contact us if a verifier misbehaves and reduces the chance of
    soft-blocks during traffic spikes — both arXiv ToS and government
    APIs (TRDizin) treat default aiohttp UAs as suspicious bot traffic.
    """
    email = get_polite_pool_email()
    if email:
        ua = f"AtfiMemnu/2.17 (Reference Search and Verification; mailto:{email})"
    else:
        ua = "AtfiMemnu/2.17 (Reference Search and Verification)"
    return {"User-Agent": ua}


async def acquire_or_rate_limited(host: str, max_wait: float) -> None:
    """Acquire a rate-limit slot or fail fast as ``RateLimitedError``.

    Wraps ``rate_limiter.acquire(host)`` in ``asyncio.wait_for(max_wait)``.
    When the inter-request pacing queue is so deep that the wait would
    exceed ``max_wait`` seconds, raise ``RateLimitedError`` so the
    orchestrator paints a ``rate_limited`` dot instead of burning the
    full search-timeout asleep and emitting a misleading ``timeout``.
    """
    try:
        await asyncio.wait_for(rate_limiter.acquire(host), timeout=max_wait)
    except asyncio.TimeoutError:
        raise RateLimitedError(host, retry_after=max_wait, status=429) from None


def get_session() -> aiohttp.ClientSession:
    """Return the process-wide verifier session, creating it on first use."""
    global _session
    if _session is None or _session.closed:
        connector = aiohttp.TCPConnector(ssl=create_ssl_context())
        _session = aiohttp.ClientSession(
            timeout=get_client_timeout(), connector=connector
        )
    return _session


async def close_session() -> None:
    """Close the shared session. Call from app shutdown."""
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def fetch_with_year_fallback(
    fetch_fn: Callable[[dict[str, str]], Awaitable[T | None]],
    params: dict[str, str],
    year_keys: Iterable[str],
) -> T | None:
    """Run a search with year-restricted params; if it produced nothing, retry
    once after stripping the year-related keys.

    Several verifiers (OpenAlex, OpenAIRE, Semantic Scholar) share this exact
    pattern: a ±1-year filter is applied for disambiguation, but a mis-parsed
    source year would silently exclude the correct paper. The single retry
    without the year keys recovers those cases without doubling traffic on
    the common path where the first call already returned hits.
    """
    best = await fetch_fn(params)
    drop = set(year_keys)
    if best is None and drop & params.keys():
        params_no_year = {k: v for k, v in params.items() if k not in drop}
        best = await fetch_fn(params_no_year)
    return best
