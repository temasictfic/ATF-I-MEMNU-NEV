"""Per-domain rate limiter using token bucket algorithm."""

import asyncio
import collections
import time


class SlidingWindowQuota:
    """Cap requests within a rolling time window.

    Pacing alone (the token bucket below) cannot enforce hour-window or
    multi-minute caps — a 1.0 s gap technically permits 3,600 req/hr, which
    blows past OpenAIRE's anonymous 60/hr budget 60× over. This class
    tracks request timestamps in a deque and refuses to admit a new one
    when the deque is at capacity within the window. Used preventively
    (before the request is sent), in contrast with the reactive 429-park
    path which only kicks in after the server has already counted us.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.timestamps: collections.deque[float] = collections.deque()

    def consume(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_requests:
            return False
        self.timestamps.append(now)
        return True

    def seconds_until_slot(self) -> float:
        if not self.timestamps:
            return 0.0
        return max(0.0, self.timestamps[0] + self.window_seconds - time.monotonic())


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, dict] = {}
        self._parked_until: dict[str, float] = {}
        self._windows: dict[str, SlidingWindowQuota] = {}
        self._lock = asyncio.Lock()

        # Default rates: seconds between requests per host.
        # Tuned to stay under each API's documented unauthenticated limit
        # with 3 concurrent sources making sequential requests. Users with
        # API keys get higher effective throughput because the keyed call
        # path on S2 / PubMed is already well under the paced rate.
        self._rates = {
            "scholar.google.com": 2.0,
            # arXiv API User's Manual explicitly asks for ≤1 req / 3 s.
            # Earlier we used 1.5 s to minimize pacing latency, but under
            # sustained 3-way source concurrency Fastly still fingerprints
            # and 429s us — 3.0 s is the only rate that actually respects
            # the documented cap.
            "export.arxiv.org": 3.0,
            # Semantic Scholar anonymous pool is a *globally shared* bucket
            # across all unauthenticated users, so per-IP pacing mostly
            # shapes burst spikes rather than avoiding 429s. 1.0 s matches
            # the keyed per-user cap and lifts effective throughput ~2×
            # over the previous blanket 2.0 s default, which was tuned
            # against three-way concurrent bursts that the rotation
            # upgrade has since cut to at most two.
            "api.semanticscholar.org": 1.0,
            # NCBI E-utilities: 3 req/s unauthenticated. 0.4 s ≈ 2.5/s.
            "eutils.ncbi.nlm.nih.gov": 0.4,
            # Crossref (Dec 2025 rate-limit regime): list/query endpoints
            # are capped at 1 req/s anonymous or 3 req/s polite. Our
            # Crossref verifier uses list/query for every search, so this
            # host-level default targets the anonymous ceiling (1.0 s).
            # Verifiers that know the user is in the polite pool pass a
            # faster `rate=` override to `acquire()`.
            "api.crossref.org": 1.0,
            # OpenAlex polite pool: 10 req/s advertised. 0.15 s ≈ 6.6/s
            # gives headroom for Cloudflare's burst shaping.
            "api.openalex.org": 0.15,
            # OpenAIRE Graph API: no published per-IP cap. The anonymous
            # ceiling is 60 req/hour (sliding), which simple inter-request
            # pacing can't enforce; we keep a 1.0 s default as the fastest
            # safe cadence for short bursts and rely on 429-park cycles to
            # keep us under the hour cap. Authenticated users pass a
            # faster 0.5 s override (7,200 req/hr → 2 req/s) from the
            # verifier via `acquire(rate=...)`.
            "api.openaire.eu": 1.0,
            # Europe PMC publishes no hard per-IP limit and asks only that
            # clients identify themselves and avoid "abusive" patterns.
            # 0.5 s ≈ 2 req/s from a single polite client is well inside
            # that envelope, and the new two-way rotation burst (down from
            # three) means this no longer needs the conservative 2.0 s
            # blanket default.
            "www.ebi.ac.uk": 0.5,
            # Open Library documents ~100 req/min. 0.6 s ≈ 1.6 req/s keeps
            # us under that with headroom for bursts.
            "openlibrary.org": 0.6,
        }
        self._default_rate = 2.0

    def park(self, domain: str, seconds: float) -> None:
        """Park a domain for ``seconds``, blocking new acquires until expiry.

        Called when a verifier observes a 429 response; honors the server's
        Retry-After so subsequent requests back off instead of pounding the
        same rate limit. Re-parking with a longer window extends; a shorter
        window is ignored so an earlier 429 can't be shortened by a later
        polite response.
        """
        if seconds <= 0:
            return
        expiry = time.monotonic() + seconds
        current = self._parked_until.get(domain, 0.0)
        if expiry > current:
            self._parked_until[domain] = expiry

    def rate_for(self, domain: str) -> float:
        """Return the configured inter-request gap (seconds) for a domain.

        Used by the orchestrator to decide whether a timed-out DB is even
        worth retrying inside the short retry window — if pacing alone is
        wider than the retry timeout, the retry is guaranteed to time out
        again before its first packet leaves the box.
        """
        return self._rates.get(domain, self._default_rate)

    def parked_remaining(self, domain: str) -> float:
        """Return seconds remaining on a park window (0.0 if not parked)."""
        expiry = self._parked_until.get(domain)
        if expiry is None:
            return 0.0
        remaining = expiry - time.monotonic()
        if remaining <= 0:
            self._parked_until.pop(domain, None)
            return 0.0
        return remaining

    def register_window(
        self, domain: str, max_requests: int, window_seconds: float
    ) -> None:
        """Register or replace a sliding-window quota for ``domain``.

        Idempotent and safe to call repeatedly — used by OpenAIRE to swap
        the anonymous (60/hr) cap for the authenticated (7,200/hr) cap when
        the user adds or rotates a refresh token, without restarting the
        process. The replacement preserves no history; it starts a fresh
        window so a previously full anon-bucket doesn't strand an authed
        user behind 60 dead timestamps.
        """
        self._windows[domain] = SlidingWindowQuota(max_requests, window_seconds)

    def consume_window(self, domain: str) -> bool:
        """Consume one slot from the window quota; True if no window registered.

        Default-allow keeps verifiers without window caps (most of them)
        completely unaffected. The window composes with ``acquire()`` —
        both gates must pass before a request fires.
        """
        window = self._windows.get(domain)
        if window is None:
            return True
        return window.consume()

    def window_seconds_until_slot(self, domain: str) -> float:
        """Seconds until the next slot frees up (0.0 if no window or empty)."""
        window = self._windows.get(domain)
        if window is None:
            return 0.0
        return window.seconds_until_slot()

    async def acquire(self, domain: str, *, rate: float | None = None):
        """Wait for the inter-request pacing window for this domain.

        ``rate`` overrides the default per-domain pacing when the caller
        has context the limiter doesn't — e.g. Crossref's polite pool
        allows a 3× faster cadence than the anonymous pool, so the
        Crossref verifier passes ``rate=0.35`` only when it has detected
        a configured contact email.

        Note: parks (from 429 / Retry-After) are *not* waited out here.
        Sleeping through a multi-second park would push the verifier past
        its per-DB search timeout and turn a transient rate limit into a
        hard failure. Parks are instead enforced by ``check_parked_url``
        at each request site, which fails fast so the source is marked
        ``rate_limited`` immediately and the next DB can proceed.
        """
        if rate is None:
            rate = self._rates.get(domain, self._default_rate)

        async with self._lock:
            bucket = self._buckets.get(domain)
            now = time.monotonic()

            if bucket is None:
                self._buckets[domain] = {"last_request": now}
                return

            elapsed = now - bucket["last_request"]
            if elapsed < rate:
                wait_time = rate - elapsed
                self._buckets[domain]["last_request"] = now + wait_time
            else:
                self._buckets[domain]["last_request"] = now
                return

        # Wait outside the lock
        if elapsed < rate:
            await asyncio.sleep(rate - elapsed)


rate_limiter = RateLimiter()
