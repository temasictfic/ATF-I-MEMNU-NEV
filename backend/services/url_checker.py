"""URL liveness checking for non-DOI/arXiv links in sources.

DOI and arXiv URLs are validated via their respective APIs (Crossref, arXiv),
so we skip them here.  For all other URLs we send a HEAD (then GET as fallback)
and treat any response with status < 400 as alive.

URLs come from PDFs — i.e. untrusted input — so before issuing any request we
validate the scheme, port, and resolved IP address. This blocks SSRF attempts
that would otherwise let a crafted PDF probe localhost, private/link-local
networks, or cloud-metadata services from inside the user's machine. Every
redirect target is re-validated; the redirect chain is capped.
"""

import asyncio
import ipaddress
import logging
from urllib.parse import urljoin, urlparse

import aiohttp
from verifiers._http import build_headers, create_ssl_context

logger = logging.getLogger(__name__)


_ALLOWED_SCHEMES = {"http", "https"}
# Allowed network ports. `None` = scheme default (80/443). Anything else is
# refused so a citation can't direct the checker at internal services on
# unusual ports (e.g. 6379 redis, 9200 elasticsearch, 8500 consul).
_ALLOWED_PORTS = {None, 80, 443, 8080, 8443}
_MAX_REDIRECTS = 5


def _build_headers() -> dict[str, str]:
    """Return liveness-check headers — shared polite-pool UA plus ``Accept: */*``.

    Reusing the verifier UA keeps the polite-pool advertising consistent across
    API and liveness traffic, so a server admin who whitelists or rate-shapes
    one channel sees the same client identity on the other.
    """
    return {**build_headers(), "Accept": "*/*"}


def is_doi_or_arxiv_url(url: str) -> bool:
    """Return True if the URL points to doi.org or arxiv.org."""
    if not url:
        return False
    lower = url.lower()
    return "doi.org" in lower or "arxiv.org" in lower


async def _is_url_safe(url: str) -> bool:
    """SSRF guard. Reject anything that isn't an http(s) URL on a normal port,
    or that resolves to a non-routable / private / loopback / link-local /
    multicast / reserved / unspecified address."""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if parsed.scheme not in _ALLOWED_SCHEMES:
        return False
    if not parsed.hostname:
        return False
    try:
        port = parsed.port
    except (ValueError, TypeError):
        return False
    if port not in _ALLOWED_PORTS:
        return False
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(parsed.hostname, None)
    except (OSError, asyncio.CancelledError):
        return False
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            return False
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return False
    return True


async def _follow_safe(
    session: aiohttp.ClientSession, method: str, url: str
) -> int | None:
    """Issue `method url` and follow up to _MAX_REDIRECTS redirects, validating
    every hop with _is_url_safe. Returns the final HTTP status, or None if any
    hop fails the SSRF check or the redirect chain runs too long."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not await _is_url_safe(current):
            return None
        async with session.request(method, current, allow_redirects=False) as resp:
            if resp.status < 300 or resp.status >= 400:
                return resp.status
            location = resp.headers.get("Location")
            if not location:
                return resp.status
            current = urljoin(current, location)
    return None


async def _check_url_with_session(session: aiohttp.ClientSession, url: str) -> bool:
    """Check liveness of a single URL using a shared session.

    Tries HEAD first; falls back to GET if the server rejects HEAD or returns
    a non-2xx/3xx status. Returns False on any network error, SSRF rejection,
    or timeout.
    """
    if not url:
        return False

    try:
        # HEAD attempt
        try:
            status = await _follow_safe(session, "HEAD", url)
            if status is not None and status < 400:
                return True
            # Some servers return 405/403/404 for HEAD — fall back to GET.
            # Other failure statuses we trust as "not alive".
            if status is not None and status not in (405, 403, 404):
                return False
        except aiohttp.ClientError:
            pass

        # GET fallback
        try:
            status = await _follow_safe(session, "GET", url)
            return status is not None and status < 400
        except aiohttp.ClientError:
            return False
    except asyncio.TimeoutError:
        logger.debug("URL liveness timeout: %s", url)
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("URL liveness error for %s: %s", url, e)
        return False


async def check_urls(urls: list[str], timeout: float = 10.0) -> dict[str, bool]:
    """Check liveness of multiple URLs in parallel.

    DOI/arXiv URLs are skipped (validated separately by API verifiers).
    Returns {url: alive} for the URLs that were actually checked.

    A single aiohttp.ClientSession is shared across all URLs in this call so
    we reuse the connection pool instead of paying a TLS handshake per URL.
    """
    targets = [u for u in urls if u and not is_doi_or_arxiv_url(u)]
    if not targets:
        return {}

    client_timeout = aiohttp.ClientTimeout(total=timeout)
    connector = aiohttp.TCPConnector(ssl=create_ssl_context())
    async with aiohttp.ClientSession(
        timeout=client_timeout, headers=_build_headers(), connector=connector
    ) as session:
        results = await asyncio.gather(
            *(_check_url_with_session(session, u) for u in targets),
            return_exceptions=True,
        )

    out: dict[str, bool] = {}
    for url, alive in zip(targets, results):
        out[url] = bool(alive) if not isinstance(alive, Exception) else False
    return out
