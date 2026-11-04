"""Orchestrate parallel verification of sources across multiple academic databases."""

import asyncio
import inspect
from typing import Any

from api.websocket import manager
from models.settings import DatabaseConfig
from models.source import ParsedSource, SourceRectangle
from models.verification_result import MatchResult, VerificationResult
from scrapers.rate_limiter import rate_limiter
from utils.text_cleaning import strip_source_noise
from verifiers._http import RateLimitedError, UnauthorizedError

from services.cache_store import save_verify_cache
from services.match_scorer import (
    classify_decision,
    determine_verification_status,
    score_to_band,
)
from services.scoring_constants import (
    LOW_PARSE_CONFIDENCE_THRESHOLD,
    STATUS_MEDIUM_THRESHOLD,
    TITLE_MATCH_THRESHOLD,
)
from services.search_settings import (
    get_max_concurrent_apis,
    get_max_concurrent_sources_per_pdf,
    get_search_timeout_seconds,
)
from services.search_urls import build_google_urls, build_search_url
from services.settings_store import get_current_settings
from services.source_extractor import extract_source_fields
from services.url_checker import check_urls, is_doi_or_arxiv_url

# Short-timeout retry pass: DBs that timed out on the main pass get one more
# shot with a tight ceiling. 5 s is long enough for a healthy API to respond
# (most DBs return in well under a second normally) but short enough that a
# source with several timed-out DBs still finishes within a couple of seconds
# of extra latency in the worst case.
_RETRY_SEARCH_TIMEOUT_SECONDS = 5


# Map DB id → rate-limiter host, used to check park state when deciding
# whether to retry a rate-limited DB. Must stay in sync with the URL each
# verifier hits (derived from each verifier's module-level ``_HOST`` /
# ``*_API`` constant — see ``check_parked_url`` in verifiers/_http.py for
# the hostname extraction the limiter actually sees).
_DB_ID_TO_HOST = {
    "crossref": "api.crossref.org",
    "openalex": "api.openalex.org",
    "openaire": "api.openaire.eu",
    "arxiv": "export.arxiv.org",
    "semantic_scholar": "api.semanticscholar.org",
    "europe_pmc": "www.ebi.ac.uk",
    "trdizin": "search.trdizin.gov.tr",
    "pubmed": "eutils.ncbi.nlm.nih.gov",
    "open_library": "openlibrary.org",
    "base": "api.base-search.net",
    "wos": "api.clarivate.com",
}


# --- Cancellation registry ---
# Maps key (pdf_id or source_id) to list of asyncio.Task objects.
# Cancelling = calling .cancel() on the tasks for immediate interruption.
_active_tasks: dict[str, list[asyncio.Task]] = {}
# Latest run allowed to mutate each source's shared result entry. This prevents
# a cancelled/obsolete run from overwriting a replacement run that started
# before the old task reached its finally block.
_active_source_jobs: dict[tuple[str, str], str] = {}


def request_cancel(key: str) -> None:
    """Cancel all tasks associated with a key (pdf_id or source_id)."""
    for task in _active_tasks.get(key, []):
        task.cancel()


def cancel_all_active() -> None:
    """Cancel all running verification tasks."""
    for tasks in _active_tasks.values():
        for task in tasks:
            task.cancel()


def _register_task(key: str, task: asyncio.Task) -> None:
    _active_tasks.setdefault(key, []).append(task)


def _unregister_tasks(key: str, tasks: list[asyncio.Task]) -> None:
    """Remove only this run's tasks without unregistering overlapping work."""
    registered = _active_tasks.get(key)
    if not registered:
        return
    removed = set(tasks)
    remaining = [task for task in registered if task not in removed]
    if remaining:
        _active_tasks[key] = remaining
    else:
        _active_tasks.pop(key, None)


def _query_for(parsed: ParsedSource) -> str:
    """Pick the manual-search query string from a parsed source.

    Prefers the NER-extracted title; falls back to the first 200 chars of
    raw text when the title is missing. Used for fallback search_urls and
    Google/Scholar URL construction.
    """
    return parsed.title or parsed.raw_text[:200]


def _supports_api_key_argument(search_fn: Any) -> bool:
    """Check whether a verifier accepts an ``api_key`` keyword argument."""
    try:
        return "api_key" in inspect.signature(search_fn).parameters
    except (TypeError, ValueError):
        return False


def _db_park_cleared(db_id: str) -> bool:
    """Return True if this DB's rate-limiter park window has fully elapsed.

    Used by the retry pass to decide whether a rate-limited DB is safe to
    re-try right now. If the host is still parked, a retry would just
    fail-fast via ``check_parked_url`` and waste a task slot. Unknown
    db_ids (missing from :data:`_DB_ID_TO_HOST`) are treated as cleared:
    we have no way to check, so we let the retry try and the call will
    either succeed or fall back to its own error path.
    """
    host = _DB_ID_TO_HOST.get(db_id)
    if host is None:
        return True
    return rate_limiter.parked_remaining(host) <= 0.0


def _db_pacing_fits_retry(db_id: str) -> bool:
    """Return True if the DB's inter-request gap fits in the retry window.

    A DB whose pacing is already wider than ``_RETRY_SEARCH_TIMEOUT_SECONDS``
    (e.g. arXiv at 3 s pacing under a deep queue) will deterministically
    time out again inside the 5 s retry budget — the rate-limiter alone
    consumes the slot. Excluding such DBs keeps the retry log quiet and
    avoids re-emitting the same misleading ``timeout`` dot.
    """
    host = _DB_ID_TO_HOST.get(db_id)
    if host is None:
        return True
    return rate_limiter.rate_for(host) < _RETRY_SEARCH_TIMEOUT_SECONDS


def _is_strong_match(result: MatchResult | None) -> bool:
    """Return True when a single verifier's result is strong enough to
    cancel the remaining parallel verifiers for this source.

    Composite is clamped to ``[0.0, 1.0]`` in ``score_match``, so a
    score of 1.0 means the title+author base plus all applicable field
    bonuses saturated. The ``title_similarity`` floor guards against
    the clamp masking a fuzzy title: bonuses (year + venue + DOI) total
    0.30 and can carry a title as weak as ~0.75 to a saturated score.
    Requiring the title to clear the !title chip threshold ensures the
    candidate is unambiguously about the same paper before we cancel
    the remaining 8 DB queries.

    User-facing toggle: ``strong_match_enabled`` in settings disables
    this short-circuit entirely, forcing every enabled DB to be queried
    even when an early result already proves the citation.
    """
    if not get_current_settings().strong_match_enabled:
        return False
    if result is None:
        return False
    if result.match_details.title_similarity < TITLE_MATCH_THRESHOLD:
        return False
    return result.score >= 1.0


async def verify_pdf_sources(
    pdf_id: str,
    sources: list[SourceRectangle],
    results_store: dict[str, dict[str, VerificationResult]],
    job_id: str = "",
):
    """Verify all sources for a single PDF.

    ``job_id`` flows through to every WS broadcast so the renderer can drop
    stale events from a superseded run (cancel-then-reverify race).
    """
    if pdf_id not in results_store:
        results_store[pdf_id] = {}

    await manager.send_log("info", f"Verifying {len(sources)} sources for {pdf_id}", pdf_id=pdf_id)

    source_semaphore = asyncio.Semaphore(get_max_concurrent_sources_per_pdf())

    async def verify_with_limit(source: SourceRectangle, index: int):
        async with source_semaphore:
            await _verify_source(pdf_id, source, results_store, index, job_id)

    # Create tasks and register for cancellation
    asyncio_tasks = [asyncio.create_task(verify_with_limit(s, i)) for i, s in enumerate(sources)]
    for t in asyncio_tasks:
        _register_task(pdf_id, t)

    try:
        await asyncio.gather(*asyncio_tasks, return_exceptions=True)
    except asyncio.CancelledError:
        # Cancel all child tasks on cancellation
        for t in asyncio_tasks:
            t.cancel()
        # Wait for children to restore/remove their transient in-progress rows.
        await asyncio.gather(*asyncio_tasks, return_exceptions=True)
    finally:
        _unregister_tasks(pdf_id, asyncio_tasks)

        # Calculate final counts (works for both normal and cancelled runs)
        results = results_store.get(pdf_id, {})
        high = sum(1 for r in results.values() if r.status == "high")
        medium = sum(1 for r in results.values() if r.status == "medium")
        low = sum(1 for r in results.values() if r.status == "low")

        await manager.broadcast("verify_pdf_done", {
            "pdf_id": pdf_id,
            "high": high,
            "medium": medium,
            "low": low,
            "job_id": job_id,
        })

        # Persist results to disk cache
        try:
            save_verify_cache(pdf_id, results)
        except Exception:  # noqa: BLE001, S110
            pass


async def verify_single_source(
    pdf_id: str,
    source_id: str,
    text: str,
    results_store: dict[str, dict[str, VerificationResult]],
    job_id: str = "",
):
    """Verify a single source (used for individual verification)."""
    if pdf_id not in results_store:
        results_store[pdf_id] = {}

    # Create a temporary SourceRectangle-like object
    from models.source import BoundingBox
    temp_source = SourceRectangle(
        id=source_id,
        pdf_id=pdf_id,
        bbox=BoundingBox(x0=0, y0=0, x1=0, y1=0, page=0),
        text=text,
        status="approved",
    )

    try:
        await _verify_source(pdf_id, temp_source, results_store, 0, job_id)
    except asyncio.CancelledError:
        pass
    finally:
        current_task = asyncio.current_task()
        if current_task is not None:
            _unregister_tasks(source_id, [current_task])

    # Recalculate counts
    results = results_store.get(pdf_id, {})
    high = sum(1 for r in results.values() if r.status == "high")
    medium = sum(1 for r in results.values() if r.status == "medium")
    low = sum(1 for r in results.values() if r.status == "low")

    await manager.broadcast("verify_pdf_updated", {
        "pdf_id": pdf_id,
        "high": high,
        "medium": medium,
        "low": low,
        "job_id": job_id,
    })

    # Persist updated results to disk cache
    try:
        save_verify_cache(pdf_id, results)
    except Exception:  # noqa: BLE001, S110
        pass


async def _verify_source(
    pdf_id: str,
    source: SourceRectangle,
    results_store: dict[str, dict[str, VerificationResult]],
    source_index: int = 0,
    job_id: str = "",
):
    """Verify a single source against all databases."""
    source_id = source.id
    source_key = (pdf_id, source_id)
    previous_result = results_store[pdf_id].get(source_id)
    _active_source_jobs[source_key] = job_id

    # Initialize result
    results_store[pdf_id][source_id] = VerificationResult(
        source_id=source_id,
        status="in_progress",
    )

    source_text = strip_source_noise(source.text)

    await manager.broadcast("verify_started", {
        "pdf_id": pdf_id,
        "source_id": source_id,
        "source_text": source_text[:200],
        "job_id": job_id,
    })

    all_matches: list[MatchResult] = []
    databases_searched: list[str] = []
    parsed: ParsedSource | None = None
    url_liveness: dict[str, bool] = {}
    # Tracked here (not inside `try`) so the `finally` block can reach it
    # even if cancellation lands before we assign inside the try.
    url_check_task: asyncio.Task | None = None
    cancelled = False

    try:
        # Load settings for API keys and enabled databases
        app_settings = get_current_settings()
        api_keys = app_settings.api_keys
        enabled_db_configs = {db.id: db for db in app_settings.databases if db.enabled}
        search_timeout = get_search_timeout_seconds()
        api_semaphore = asyncio.Semaphore(get_max_concurrent_apis())

        # Parse source text into structured fields
        parsed = await extract_source_fields(source_text)

        if parsed.doi:
            await manager.send_log("info", f"DOI found: {parsed.doi}", pdf_id=pdf_id, source_id=source_id)

        if parsed.citation_format:
            await manager.send_log("info", f"Format: {parsed.citation_format}", pdf_id=pdf_id, source_id=source_id)

        # All verifier searches are driven by the NER-extracted title. When the
        # parse confidence is low we warn but do NOT overwrite the title with
        # cleaned raw text — the user has explicitly required that every DB
        # query use the NER title, falling back only when it is genuinely empty.
        if parsed.parse_confidence < LOW_PARSE_CONFIDENCE_THRESHOLD:
            await manager.send_log(
                "warning",
                f"Low parse confidence ({parsed.parse_confidence:.2f}), searching with extracted title only",
                pdf_id=pdf_id,
                source_id=source_id,
            )

        # Kick off URL liveness check in parallel with the database searches.
        # Skips doi.org / arxiv.org URLs (validated via API verifiers).
        urls_to_check: list[str] = []
        if parsed.url and not is_doi_or_arxiv_url(parsed.url):
            urls_to_check.append(parsed.url)
        if urls_to_check:
            url_check_task = asyncio.create_task(check_urls(urls_to_check))

        # Phase 1: Tier 1 APIs (parallel)
        tier1_results = await _run_tier1_apis(
            pdf_id,
            source_id,
            parsed,
            api_keys,
            enabled_db_configs,
            api_semaphore,
            search_timeout,
            source_index,
            job_id=job_id,
        )
        all_matches.extend(tier1_results["matches"])
        databases_searched.extend(tier1_results["searched"])

        # Short-timeout retry pass for DBs that timed out on the main pass,
        # plus rate-limited DBs whose park has already cleared by the time
        # we get here. Most timeouts in the wild are transient contention
        # rather than the API actually being down, and the new stride
        # rotation has cut peak per-DB concurrency from 3 to 2 — a second
        # attempt lands in a quieter network window more often than not.
        # For rate-limited DBs the retry is opportunistic: if the 429 came
        # from *this* source's request the park almost certainly has not
        # cleared yet, but if it came from an *earlier* source bleeding
        # into ours via ``check_parked_url``, the park is usually 3-6 s
        # old by now and often cleared (new 10 s default). Park-still-
        # active entries stay in the skip set — a retry would just re-
        # trigger the fail-fast and waste a task slot.
        #
        # Short 5 s ceiling (vs the main-pass ~20 s) keeps worst-case
        # retry latency bounded even if everything fails again.
        retriable_timeouts = {
            db_id
            for db_id in tier1_results["timeouts"]
            if _db_pacing_fits_retry(db_id)
        }
        retriable_rate_limited = {
            db_id
            for db_id in tier1_results["rate_limited"]
            if _db_park_cleared(db_id)
        }
        retry_set = retriable_timeouts | retriable_rate_limited

        if retry_set and not _is_strong_match(
            max(all_matches, key=lambda m: m.score, default=None)
        ):
            await manager.send_log(
                "info",
                (
                    f"Retrying {len(retry_set)} DB(s) with short timeout"
                    f" (timeout={len(retriable_timeouts)},"
                    f" rate-limited-cleared={len(retriable_rate_limited)})"
                ),
                pdf_id=pdf_id,
                source_id=source_id,
            )
            retry_results = await _run_tier1_apis(
                pdf_id,
                source_id,
                parsed,
                api_keys,
                enabled_db_configs,
                api_semaphore,
                _RETRY_SEARCH_TIMEOUT_SECONDS,
                source_index,
                restricted_db_ids=retry_set,
                job_id=job_id,
            )
            all_matches.extend(retry_results["matches"])
            # Don't re-extend `databases_searched` — the DB names are
            # already present from the main pass, and adding them again
            # would double-count in any downstream consumer that treats
            # the list as a set.

        # Wait for URL check to complete (with bounded timeout)
        if url_check_task is not None:
            try:
                url_liveness = await asyncio.wait_for(url_check_task, timeout=12.0)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                url_liveness = {u: False for u in urls_to_check}

    except asyncio.CancelledError:
        cancelled = True
        await manager.send_log("info", f"Source {source_id} verification cancelled",
                              pdf_id=pdf_id, source_id=source_id)
    except Exception as e:  # noqa: BLE001
        await manager.send_log("error", f"Source {source_id} verification error: {e}",
                              pdf_id=pdf_id, source_id=source_id)
    finally:
        # Cancel the fire-and-forget URL liveness check so it stops making
        # HTTP requests as soon as the source-level task is cancelled. Without
        # this, `check_urls` runs to its ~10s timeout in the background even
        # after the user has clicked Stop.
        if url_check_task is not None and not url_check_task.done():
            url_check_task.cancel()
            try:
                await url_check_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
                pass

        # A replacement run may already own this source. Never let the older
        # task overwrite its in-progress or settled result.
        if _active_source_jobs.get(source_key) == job_id:
            if cancelled:
                # Cancellation is not a bibliographic judgment. Restore the prior
                # settled row, or remove a first-run placeholder, instead of
                # finalizing partial work as Low/Fabricated and caching it.
                if previous_result is not None and previous_result.status != "in_progress":
                    results_store[pdf_id][source_id] = previous_result
                else:
                    results_store[pdf_id].pop(source_id, None)
                _active_source_jobs.pop(source_key, None)
            else:
                await _finalize_result(
                    results_store, pdf_id, source_id, parsed, all_matches,
                    databases_searched, url_liveness, job_id=job_id,
                )
                if _active_source_jobs.get(source_key) == job_id:
                    _active_source_jobs.pop(source_key, None)


async def _run_tier1_apis(
    pdf_id: str,
    source_id: str,
    parsed: ParsedSource,
    api_keys: dict[str, str],
    enabled_db_configs: dict[str, DatabaseConfig],
    api_semaphore: asyncio.Semaphore,
    search_timeout: int,
    source_index: int = 0,
    restricted_db_ids: set[str] | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """Run Tier 1 API verifiers in parallel.

    ``restricted_db_ids`` filters the verifier list to just those ids
    (used by the short-timeout retry pass to re-run only the DBs that
    timed out in the main phase). When ``None`` every enabled DB runs.
    Return dict also carries ``timeouts`` — the list of db_ids whose
    main-pass search raised ``TimeoutError`` — so callers can drive the
    retry phase without re-deriving that state from event logs.
    """
    from verifiers.arxiv import search as arxiv_search
    from verifiers.base import search as base_search
    from verifiers.crossref import search as crossref_search
    from verifiers.europe_pmc import search as europe_pmc_search
    from verifiers.open_library import search as open_library_search
    from verifiers.openaire import search as openaire_search
    from verifiers.openalex import search as openalex_search
    from verifiers.pubmed import search as pubmed_search
    from verifiers.semantic_scholar import search as semantic_search
    from verifiers.trdizin import search as trdizin_search
    from verifiers.wos import search as wos_search

    # Map database id → (display name, search function)
    verifier_registry = {
        "crossref": ("Crossref", crossref_search),
        "openalex": ("OpenAlex", openalex_search),
        "openaire": ("OpenAIRE", openaire_search),
        "arxiv": ("arXiv", arxiv_search),
        "semantic_scholar": ("Semantic Scholar", semantic_search),
        "europe_pmc": ("Europe PMC", europe_pmc_search),
        "trdizin": ("TRDizin", trdizin_search),
        "pubmed": ("PubMed", pubmed_search),
        "open_library": ("Open Library", open_library_search),
        "base": ("BASE", base_search),
        "wos": ("Web of Science", wos_search),
    }

    # Build verifier list in the order defined by user settings. The
    # optional ``restricted_db_ids`` filter is how the retry pass asks
    # for a subset — rotation still applies so retries inherit the same
    # burst-spreading behaviour as the main pass.
    verifiers = []
    for db_id in enabled_db_configs:
        if restricted_db_ids is not None and db_id not in restricted_db_ids:
            continue
        entry = verifier_registry.get(db_id)
        if entry:
            verifiers.append((db_id, entry[0], entry[1]))

    # Rotate by source index so concurrent sources start on different DBs,
    # avoiding per-DB rate-limit bursts from lockstep traversal.
    #
    # Stride matters: with the default 3 concurrent sources and a 5-slot
    # per-source API semaphore, a stride of 1 leaves the *middle* third of
    # the database list inside every source's first window, so those DBs
    # get hit three times simultaneously while the DBs at the ends of the
    # list sit idle. Using stride = ⌊N / concurrency⌋ spaces the starting
    # positions evenly across the list, which flattens the initial-burst
    # distribution to a maximum of 2 concurrent hits per DB (the pigeonhole
    # lower bound: 3 sources × 5 slots = 15 calls over 9 DBs = 1.67 avg).
    if verifiers:
        concurrency = max(1, get_max_concurrent_sources_per_pdf())
        stride = max(1, len(verifiers) // concurrency)
        offset = (source_index * stride) % len(verifiers)
        verifiers = verifiers[offset:] + verifiers[:offset]

    matches: list[MatchResult] = []
    searched: list[str] = []
    timeouts: list[str] = []  # db_ids whose search timed out — fed to retry pass
    rate_limited: list[str] = []  # db_ids that returned 429 or hit a parked host

    async def run_verifier(db_id: str, name: str, search_fn: Any):
        fallback_url = build_search_url(name, _query_for(parsed))
        try:
            async with api_semaphore:
                api_key_names = {
                    "openalex": "openalex",
                    "semantic_scholar": "semantic_scholar",
                    "pubmed": "pubmed",
                    "base": "base",
                    "wos": "wos",
                }
                api_key_name = api_key_names.get(db_id)
                api_key = (api_keys.get(api_key_name, "") or "").strip() if api_key_name else None
                try:
                    await manager.broadcast("verify_db_checking", {
                        "pdf_id": pdf_id, "source_id": source_id, "database": name,
                        "job_id": job_id,
                    })

                    if _supports_api_key_argument(search_fn):
                        result = await asyncio.wait_for(
                            search_fn(parsed, api_key=api_key or None),
                            timeout=search_timeout,
                        )
                    else:
                        result = await asyncio.wait_for(search_fn(parsed), timeout=search_timeout)

                    searched.append(name)
                    score = result.score if result is not None else 0.0
                    found = score >= STATUS_MEDIUM_THRESHOLD

                    await manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": found,
                        "match": result.model_dump() if result else None,
                        "db_status": score_to_band(score) if result is not None else "no_match",
                        "search_url": (result.search_url if result else None) or fallback_url,
                        "job_id": job_id,
                    })

                    if result and result.score > 0:
                        matches.append(result)
                except asyncio.TimeoutError:
                    searched.append(name)
                    timeouts.append(db_id)
                    await manager.send_log("warning", f"{name} timed out",
                                          pdf_id=pdf_id, source_id=source_id, database=name)
                    await manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": False,
                        "db_status": "timeout",
                        "error_message": f"Timeout after {search_timeout}s",
                        "search_url": fallback_url,
                        "job_id": job_id,
                    })
                except RateLimitedError as e:
                    searched.append(name)
                    rate_limited.append(db_id)
                    retry_msg = (
                        f" (retry-after {e.retry_after:.0f}s)"
                        if e.retry_after is not None else ""
                    )
                    await manager.send_log(
                        "warning",
                        f"{name} rate limited{retry_msg}",
                        pdf_id=pdf_id, source_id=source_id, database=name,
                    )
                    await manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": False,
                        "db_status": "rate_limited",
                        "retry_after": e.retry_after,
                        "search_url": fallback_url,
                        "job_id": job_id,
                    })
                except UnauthorizedError as e:
                    searched.append(name)
                    await manager.send_log(
                        "warning",
                        f"{name} unauthorized: {e.detail or 'access denied'}",
                        pdf_id=pdf_id, source_id=source_id, database=name,
                    )
                    await manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": False,
                        "db_status": "unauthorized",
                        "error_message": (e.detail or str(e))[:200],
                        "search_url": fallback_url,
                        "job_id": job_id,
                    })
                except Exception as e:  # noqa: BLE001
                    searched.append(name)
                    await manager.send_log("warning", f"{name} search failed: {e}",
                                          pdf_id=pdf_id, source_id=source_id, database=name)
                    await manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": False,
                        "db_status": "error",
                        "error_message": str(e)[:200],
                        "search_url": fallback_url,
                        "job_id": job_id,
                    })
        except asyncio.CancelledError:
            # Short-circuit cancel: a sibling verifier landed a strong
            # DOI-exact match. Emit a "skipped" event so the UI gets a
            # terminal dot for this db. Handled at the outer level so
            # cancellations while queued on api_semaphore (before the
            # body runs) are covered too — otherwise those tasks would
            # leave empty dots. Shielded so the broadcast completes even
            # as the task is torn down.
            if name not in searched:
                searched.append(name)
                try:
                    await asyncio.shield(manager.broadcast("verify_db_checked", {
                        "pdf_id": pdf_id,
                        "source_id": source_id,
                        "database": name,
                        "found": False,
                        "db_status": "skipped",
                        "search_url": fallback_url,
                        "job_id": job_id,
                    }))
                except Exception:  # noqa: BLE001, S110
                    pass
            raise

    # Create all verifier tasks up front, then walk as_completed so we can
    # inspect results as they land and short-circuit when one verifier
    # returns a strong DOI-exact match. Siblings still in flight get
    # cancelled, their CancelledError handlers emit "skipped" dots, and
    # the finally block gathers them so cleanup completes before we
    # return. On ambiguous sources no verifier crosses the threshold
    # and every task runs to normal completion, exactly like before.
    tasks = [
        asyncio.create_task(run_verifier(db_id, name, fn))
        for db_id, name, fn in verifiers
    ]
    try:
        for fut in asyncio.as_completed(tasks):
            try:
                await fut
            except asyncio.CancelledError:
                # Re-raised from run_verifier's cancel handler after it
                # emitted the "skipped" event. Swallow here so the
                # as_completed loop keeps progressing.
                pass
            except Exception:  # noqa: BLE001, S110
                # Other exceptions are already handled inside run_verifier
                # (they broadcast their own error/timeout/rate_limited
                # events). Don't let them break the as_completed walk.
                pass

            if any(_is_strong_match(m) for m in matches):
                for t in tasks:
                    if not t.done():
                        t.cancel()
                break
    finally:
        # Wait for any still-running tasks (either cancelled short-circuit
        # tasks finishing their cleanup, or the normal-path case where we
        # broke out after the last task) to fully settle.
        await asyncio.gather(*tasks, return_exceptions=True)

    return {
        "matches": matches,
        "searched": searched,
        "timeouts": timeouts,
        "rate_limited": rate_limited,
    }


async def verify_pdf_sources_filtered(
    pdf_id: str,
    sources: list[SourceRectangle],
    results_store: dict[str, dict[str, VerificationResult]],
    custom_texts: dict[str, str],
    excluded_ids: set[str],
    job_id: str = "",
):
    """Verify sources with custom texts and exclusions."""
    filtered = [s for s in sources if s.id not in excluded_ids]

    # Apply custom text overrides
    modified = []
    for s in filtered:
        if s.id in custom_texts:
            modified.append(s.model_copy(update={"text": strip_source_noise(custom_texts[s.id])}))
        else:
            modified.append(s)

    await verify_pdf_sources(pdf_id, modified, results_store, job_id=job_id)


async def _finalize_result(
    results_store: dict,
    pdf_id: str,
    source_id: str,
    parsed: ParsedSource | None,
    all_matches: list[MatchResult],
    databases_searched: list[str],
    url_liveness: dict[str, bool] | None = None,
    job_id: str = "",
):
    """Finalize the verification result for a source.

    Picks the best candidate by composite score, then runs the new 3-category
    status determination (high / medium / low) with problem tags.
    """
    url_liveness = url_liveness or {}

    # Best candidate is ranked by the *unclamped* raw score so two saturated
    # 1.00 composites can still be ordered by underlying signal strength
    # (e.g. a candidate with all three field bonuses beats one with two).
    best_match = max(all_matches, key=lambda m: m.raw_score) if all_matches else None

    if parsed is not None:
        status, problem_tags = determine_verification_status(
            parsed, best_match, url_liveness
        )
        decision_tag = classify_decision(parsed, best_match)
    else:
        # Failure to parse → fall back to low / fabricated
        status, problem_tags = "low", []
        decision_tag = "fabricated"

    # Build Google Scholar / Google Search URLs from NER-extracted title
    parsed_title = parsed.title if parsed else ""
    scholar_url, google_url = build_google_urls(_query_for(parsed)) if parsed else ("", "")

    result = VerificationResult(
        source_id=source_id,
        status=status,
        problem_tags=problem_tags,
        decision_tag=decision_tag,
        url_liveness=url_liveness,
        best_match=best_match,
        all_results=sorted(all_matches, key=lambda m: m.raw_score, reverse=True),
        databases_searched=databases_searched,
        parsed_title=parsed_title,
        scholar_url=scholar_url,
        google_url=google_url,
    )
    results_store[pdf_id][source_id] = result

    await manager.broadcast("verify_source_done", {
        "pdf_id": pdf_id,
        "source_id": source_id,
        "status": status,
        "problem_tags": problem_tags,
        "decision_tag": decision_tag,
        "decision_tag_override": result.decision_tag_override,
        "tag_overrides": result.tag_overrides,
        "url_liveness": url_liveness,
        "best_match": best_match.model_dump() if best_match else None,
        "all_results": [m.model_dump() for m in result.all_results],
        "databases_searched": list(databases_searched),
        "scholar_url": scholar_url,
        "google_url": google_url,
        "job_id": job_id,
    })
