"""Verification endpoints.

Cache I/O and the in-memory job/result dicts have been extracted to
:mod:`services.cache_store` and :mod:`services.job_store` respectively, so
this module no longer cross-imports api.parsing or holds module-level state
that anyone else has to import. That breaks the parsing↔verification and
verification↔orchestrator cycles.
"""

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from models.source import SourceRectangle
from models.verification_result import (
    DecisionTag,
    MatchResult,
    TagKey,
)
from pydantic import BaseModel
from services.cache_store import (
    _validate_pdf_id,
    load_sources_for_pdf,
    load_verify_cache,
    save_verify_cache,
)
from services.job_store import verify_jobs, verify_results
from services.scoring_constants import LOW_PARSE_CONFIDENCE_THRESHOLD
from services.search_urls import build_search_url

from api.websocket import manager

router = APIRouter()


class VerifyRequest(BaseModel):
    pdf_ids: list[str]


class VerifySourceRequest(BaseModel):
    text: str | None = None


class OverrideRequest(BaseModel):
    # User can only override to one of the three settled bands. "pending"
    # / "in_progress" are orchestrator-only lifecycle states.
    status: Literal["high", "medium", "low"]


class TagOverrideRequest(BaseModel):
    tag: TagKey
    state: bool | None  # None clears the override


class DecisionOverrideRequest(BaseModel):
    decision: DecisionTag | None  # None clears the override


class VerifyBatchRequest(BaseModel):
    pdf_ids: list[str]
    texts: dict[str, str] = {}
    excluded_ids: list[str] = []


@router.post("/verify/batch")
async def start_batch_verification(request: VerifyBatchRequest):
    job_id = str(uuid.uuid4())[:8]
    verify_jobs[job_id] = {"status": "running", "pdfs": request.pdf_ids}

    asyncio.create_task(_verify_batch(
        job_id, request.pdf_ids, request.texts, set(request.excluded_ids)
    ))
    return {"job_id": job_id}


def _load_sources_or_empty(pdf_id: str) -> list[SourceRectangle]:
    """Load cached sources for a PDF. Used everywhere pdf_store was used before."""
    sources = load_sources_for_pdf(pdf_id)
    return sources if sources is not None else []


async def _verify_batch(job_id: str, pdf_ids: list[str], texts: dict[str, str], excluded_ids: set[str]):
    from services.verification_orchestrator import verify_pdf_sources_filtered

    await manager.send_log("info", f"Starting batch verification for {len(pdf_ids)} PDFs")

    try:
        for pid in pdf_ids:
            sources = _load_sources_or_empty(pid)
            if not sources:
                continue
            try:
                await verify_pdf_sources_filtered(
                    pid, sources, verify_results, texts, excluded_ids, job_id=job_id,
                )
            except Exception as e:  # noqa: BLE001
                await manager.send_log("error", f"Verification failed for {pid}: {e}")

        # Save results to disk cache (best-effort; cache write failure must
        # not strand the job in `running`).
        for pdf_id in pdf_ids:
            if verify_results.get(pdf_id):
                try:
                    save_verify_cache(pdf_id, verify_results[pdf_id])
                except Exception as e:  # noqa: BLE001
                    await manager.send_log("error", f"Cache write failed for {pdf_id}: {e}")

        verify_jobs[job_id]["status"] = "done"
        await manager.send_log("success", "Batch verification complete")
    except Exception as e:
        verify_jobs[job_id]["status"] = "failed"
        verify_jobs[job_id]["error"] = str(e)
        await manager.send_log("error", f"Batch verification job failed: {e}")
        raise


@router.post("/verify")
async def start_verification(request: VerifyRequest):
    job_id = str(uuid.uuid4())[:8]
    verify_jobs[job_id] = {"status": "running", "pdfs": request.pdf_ids}

    asyncio.create_task(_verify_all_pdfs(job_id, request.pdf_ids))
    return {"job_id": job_id}


async def _verify_all_pdfs(job_id: str, pdf_ids: list[str]):
    from services.verification_orchestrator import verify_pdf_sources

    await manager.send_log("info", f"Starting verification for {len(pdf_ids)} PDFs")

    try:
        for pid in pdf_ids:
            sources = _load_sources_or_empty(pid)
            if not sources:
                continue
            try:
                await verify_pdf_sources(pid, sources, verify_results, job_id=job_id)
            except Exception as e:  # noqa: BLE001
                await manager.send_log("error", f"Verification failed for {pid}: {e}")

        for pdf_id in pdf_ids:
            if verify_results.get(pdf_id):
                try:
                    save_verify_cache(pdf_id, verify_results[pdf_id])
                except Exception as e:  # noqa: BLE001
                    await manager.send_log("error", f"Cache write failed for {pdf_id}: {e}")

        verify_jobs[job_id]["status"] = "done"
        await manager.send_log("success", "Verification complete for all PDFs")
    except Exception as e:
        verify_jobs[job_id]["status"] = "failed"
        verify_jobs[job_id]["error"] = str(e)
        await manager.send_log("error", f"Verification job failed: {e}")
        raise


@router.post("/verify/pdf/{pdf_id}")
async def reverify_pdf(pdf_id: str):
    _validate_pdf_id(pdf_id)
    sources = _load_sources_or_empty(pdf_id)
    if not sources:
        raise HTTPException(status_code=404, detail="PDF not found")

    job_id = str(uuid.uuid4())[:8]
    # Register before launch so the returned job_id is pollable via
    # GET /verify/status/{job_id}. Previously this was missing, so the
    # client could poll a known-good id and get 404 back.
    verify_jobs[job_id] = {"status": "running", "pdfs": [pdf_id]}

    asyncio.create_task(_reverify_pdf_job(job_id, pdf_id, sources))
    return {"job_id": job_id}


async def _reverify_pdf_job(job_id: str, pdf_id: str, sources: list[SourceRectangle]):
    from services.verification_orchestrator import verify_pdf_sources

    try:
        await verify_pdf_sources(pdf_id, sources, verify_results, job_id=job_id)
        if verify_results.get(pdf_id):
            try:
                save_verify_cache(pdf_id, verify_results[pdf_id])
            except Exception as e:  # noqa: BLE001
                await manager.send_log("error", f"Cache write failed for {pdf_id}: {e}")
        verify_jobs[job_id]["status"] = "done"
    except Exception as e:
        verify_jobs[job_id]["status"] = "failed"
        verify_jobs[job_id]["error"] = str(e)
        await manager.send_log("error", f"Reverify job failed for {pdf_id}: {e}")
        raise


@router.post("/verify/source/{pdf_id}/{source_id}")
async def reverify_source(pdf_id: str, source_id: str, request: VerifySourceRequest):
    sources = _load_sources_or_empty(pdf_id)
    if not sources:
        raise HTTPException(status_code=404, detail="PDF not found")

    source = next((s for s in sources if s.id == source_id), None)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")

    from services.verification_orchestrator import _register_task

    # Mint a job_id for this single-source run so WS broadcasts can be tagged
    # and the renderer can drop late events from a superseded run.
    job_id = str(uuid.uuid4())[:8]
    verify_jobs[job_id] = {"status": "running", "pdfs": [pdf_id]}

    # Use updated text if provided
    text = request.text if request.text is not None else source.text
    task = asyncio.create_task(
        _reverify_source_job(job_id, pdf_id, source_id, text)
    )
    _register_task(source_id, task)
    return {"job_id": job_id}


async def _reverify_source_job(
    job_id: str,
    pdf_id: str,
    source_id: str,
    text: str,
) -> None:
    """Run a single-source job with the same terminal lifecycle as PDF jobs."""
    from services.verification_orchestrator import verify_single_source

    try:
        await verify_single_source(
            pdf_id, source_id, text, verify_results, job_id
        )
        verify_jobs[job_id]["status"] = "done"
    except asyncio.CancelledError:
        verify_jobs[job_id]["status"] = "cancelled"
        raise
    except Exception as e:  # noqa: BLE001
        verify_jobs[job_id]["status"] = "failed"
        verify_jobs[job_id]["error"] = str(e)
        await manager.send_log(
            "error",
            f"Source verification failed for {source_id}: {e}",
            pdf_id=pdf_id,
            source_id=source_id,
        )


def _persist_settled_for_pdf(pdf_id: str) -> None:
    """Snapshot already-settled (high/medium/low) results to disk on cancel.

    Why: cancel endpoints used to return immediately and leave the disk write
    to the orchestrator's `finally` block. If the user closed the app between
    Stop and that finally running, partial results were lost. Saving here
    synchronously closes that race.

    Merges in-memory settled rows over the existing disk state so we don't
    clobber prior-run results for sources that are currently in_progress
    (their disk entry stays). in_progress rows in memory are dropped — they'd
    otherwise be finalised to low/fabricated by the trailing CancelledError
    handlers, and writing those would inflate the Fabricated pill on reload.
    """
    in_mem = verify_results.get(pdf_id) or {}
    if not in_mem:
        return
    merged = load_verify_cache(pdf_id) or {}
    for sid, r in in_mem.items():
        if r.status in ("high", "medium", "low"):
            merged[sid] = r
    if merged:
        try:
            save_verify_cache(pdf_id, merged)
        except Exception:  # noqa: BLE001, S110
            pass


@router.post("/verify/cancel")
async def cancel_all_verification():
    from services.verification_orchestrator import cancel_all_active
    cancel_all_active()
    for pid in list(verify_results.keys()):
        _persist_settled_for_pdf(pid)
    await manager.send_log("info", "Verification cancelled by user")
    return {"success": True}


@router.post("/verify/cancel/pdf/{pdf_id}")
async def cancel_pdf_verification(pdf_id: str):
    from services.verification_orchestrator import request_cancel
    request_cancel(pdf_id)
    _persist_settled_for_pdf(pdf_id)
    await manager.send_log("info", f"Verification cancelled for {pdf_id}", pdf_id=pdf_id)
    return {"success": True}


@router.post("/verify/cancel/source/{source_id}")
async def cancel_source_verification(source_id: str):
    from services.verification_orchestrator import request_cancel
    request_cancel(source_id)
    # source_id alone doesn't reveal its pdf — flush every PDF that has
    # in-memory state. Cheap; each settled-row dict is small.
    for pid in list(verify_results.keys()):
        _persist_settled_for_pdf(pid)
    return {"success": True}


@router.get("/verify/status/{job_id}")
async def get_verify_status(job_id: str):
    if job_id not in verify_jobs:
        raise HTTPException(status_code=404, detail="Job not found")

    job = verify_jobs[job_id]
    job_done = job["status"] == "done"

    summaries = []
    for pdf_id in job["pdfs"]:
        results = verify_results.get(pdf_id, {})
        high = sum(1 for r in results.values() if r.status == "high")
        medium = sum(1 for r in results.values() if r.status == "medium")
        low = sum(1 for r in results.values() if r.status == "low")
        in_progress = sum(1 for r in results.values() if r.status == "in_progress")

        # A PDF is completed if: no in-progress sources AND either
        # it has results OR the entire job is done (handles excluded/empty PDFs)
        completed = in_progress == 0 and (len(results) > 0 or job_done)

        summaries.append({
            "pdf_id": pdf_id,
            "high": high,
            "medium": medium,
            "low": low,
            "in_progress": in_progress,
            "total": len(results),
            "completed": completed,
        })

    return {"pdfs": summaries}


@router.get("/verify/results/{pdf_id}")
async def get_verify_results(pdf_id: str):
    results = verify_results.get(pdf_id)
    if results is None:
        # Try loading from disk cache
        cached = load_verify_cache(pdf_id)
        if cached:
            verify_results[pdf_id] = cached
            results = cached
        else:
            results = {}
    return {"results": {k: v.model_dump() for k, v in results.items()}}


@router.put("/verify/override/{pdf_id}/{source_id}")
async def override_status(pdf_id: str, source_id: str, request: OverrideRequest):
    if pdf_id not in verify_results or source_id not in verify_results[pdf_id]:
        raise HTTPException(status_code=404, detail="Result not found")

    verify_results[pdf_id][source_id].status = request.status

    # Recalculate counts
    results = verify_results[pdf_id]
    high = sum(1 for r in results.values() if r.status == "high")
    medium = sum(1 for r in results.values() if r.status == "medium")
    low = sum(1 for r in results.values() if r.status == "low")

    await manager.broadcast("verify_pdf_updated", {
        "pdf_id": pdf_id,
        "high": high,
        "medium": medium,
        "low": low,
    })
    await manager.send_log(
        "info",
        f"Source [{source_id}] status overridden to {request.status.upper()}",
        pdf_id=pdf_id,
        source_id=source_id,
    )

    # Persist updated results to disk cache
    save_verify_cache(pdf_id, verify_results[pdf_id])

    return {"success": True}


@router.post("/verify/tag-override/{pdf_id}/{source_id}")
async def set_tag_override(pdf_id: str, source_id: str, request: TagOverrideRequest):
    """Store per-tag ON/OFF overrides for a source card.

    state=True  → force ON
    state=False → force OFF
    state=None  → clear override (revert to default-derived state)

    Changing a field tag returns decision classification to automatic mode, so
    the updated tags can determine the source's decision again.
    """
    # Pydantic's Literal[TagKey] already validates request.tag at parse time;
    # an unknown tag yields a 422 before this handler runs.
    if pdf_id not in verify_results or source_id not in verify_results[pdf_id]:
        raise HTTPException(status_code=404, detail="Result not found")

    existing = verify_results[pdf_id][source_id]
    if request.state is None:
        existing.tag_overrides.pop(request.tag, None)
    else:
        existing.tag_overrides[request.tag] = request.state
    existing.decision_tag_override = None

    save_verify_cache(pdf_id, verify_results[pdf_id])

    await manager.broadcast("verify_source_done", {
        "pdf_id": pdf_id,
        "source_id": source_id,
        "status": existing.status,
        "problem_tags": existing.problem_tags,
        "decision_tag": existing.decision_tag,
        "decision_tag_override": existing.decision_tag_override,
        "tag_overrides": existing.tag_overrides,
        "url_liveness": existing.url_liveness,
        "best_match": existing.best_match.model_dump() if existing.best_match else None,
        "all_results": [m.model_dump() for m in existing.all_results],
        "databases_searched": list(existing.databases_searched),
        "scholar_url": existing.scholar_url,
        "google_url": existing.google_url,
    })

    return {"success": True}


@router.post("/verify/decision-override/{pdf_id}/{source_id}")
async def set_decision_override(pdf_id: str, source_id: str, request: DecisionOverrideRequest):
    """Store the user's three-state decision-tag override for a source.

    decision="valid" | "citation" | "fabricated" → force that decision state
    decision=None                                → clear override (use classify_decision)
    """
    # Pydantic's `DecisionTag | None` already validates request.decision at
    # parse time; an unknown value yields a 422 before this handler runs.
    if pdf_id not in verify_results or source_id not in verify_results[pdf_id]:
        raise HTTPException(status_code=404, detail="Result not found")

    existing = verify_results[pdf_id][source_id]
    existing.decision_tag_override = request.decision
    save_verify_cache(pdf_id, verify_results[pdf_id])

    await manager.broadcast("verify_source_done", {
        "pdf_id": pdf_id,
        "source_id": source_id,
        "status": existing.status,
        "problem_tags": existing.problem_tags,
        "decision_tag": existing.decision_tag,
        "decision_tag_override": existing.decision_tag_override,
        "tag_overrides": existing.tag_overrides,
        "url_liveness": existing.url_liveness,
        "best_match": existing.best_match.model_dump() if existing.best_match else None,
        "all_results": [m.model_dump() for m in existing.all_results],
        "databases_searched": list(existing.databases_searched),
        "scholar_url": existing.scholar_url,
        "google_url": existing.google_url,
    })

    return {"success": True}


class ScholarCandidate(BaseModel):
    title: str
    authors: list[str] = []
    year: int | None = None
    doi: str | None = None
    url: str = ""
    apa_citation: str = ""
    scraped_truncated: bool = False
    cid: str = ""


class ScoreScholarRequest(BaseModel):
    pdf_id: str
    source_id: str
    source_text: str
    candidates: list[ScholarCandidate]
    # Full citation text used for parsing the source side. When the user edits
    # a card's textbox, the swapped text lives in the renderer's verifyTexts
    # store — it never makes it back into the on-disk sources cache. Without
    # this field, score_scholar would re-parse the original PDF text from the
    # sources cache and emit chip tags that disagree with the main verify run.
    full_source_text: str | None = None


@router.post("/verify/score-scholar")
async def score_scholar(request: ScoreScholarRequest):
    """Score Google Scholar candidates against a source and merge into results."""
    from services.author_matcher import clean_scholar_authors
    from services.match_scorer import (
        classify_decision,
        determine_verification_status,
        score_match,
    )
    from services.ner_extractor import extract_fields_ner
    from services.source_extractor import extract_source_fields

    # request.source_text is the title-only search query used to hit Scholar.
    # SIRIS NER was trained on full citations and mislabels bare titles — it
    # thinks the leading capitalized words are authors. Prefer the renderer-
    # supplied full citation (which honours user edits in the textbox); fall
    # back to the on-disk sources cache when an older renderer doesn't send it.
    if request.full_source_text:
        full_source_text = request.full_source_text
    else:
        sources = load_sources_for_pdf(request.pdf_id) or []
        source_rect = next((s for s in sources if s.id == request.source_id), None)
        full_source_text = source_rect.text if source_rect else request.source_text

    parsed = await extract_source_fields(full_source_text)

    scholar_matches: list[MatchResult] = []
    search_url = build_search_url("Google Scholar", request.source_text[:200])

    for cand in request.candidates:
        # If we have an APA citation from Scholar's "Cite" dialog, run it
        # through the same NER extractor used on source sources. Scraped
        # Scholar fields truncate title/authors ("J Smith, A Jones…"); the
        # APA string contains the full metadata. Falling through to scraped
        # values preserves behaviour when enrichment fails (CAPTCHA on the
        # cite dialog, low-confidence NER output, etc.).
        title = cand.title
        authors = cand.authors
        year = cand.year
        doi = cand.doi
        journal = ""
        # Bibliographic extras only land when the APA "Cite" string is
        # present and NER hits its confidence floor — otherwise leave them
        # empty (matches the `journal = ""` fallback above).
        volume: str | None = None
        issue: str | None = None
        pages: str | None = None
        publisher = ""
        issn: list[str] = []
        isbn: list[str] = []
        if cand.apa_citation:
            ner = await extract_fields_ner(cand.apa_citation)
            if ner is not None and ner.parse_confidence >= LOW_PARSE_CONFIDENCE_THRESHOLD:
                if ner.title:
                    title = ner.title
                if ner.authors:
                    authors = ner.authors
                if ner.year:
                    year = ner.year
                if ner.doi:
                    doi = ner.doi
                if ner.journal:
                    journal = ner.journal
                if ner.volume:
                    volume = ner.volume
                if ner.issue:
                    issue = ner.issue
                if ner.pages:
                    pages = ner.pages
                if ner.publisher:
                    publisher = ner.publisher
                if ner.issn:
                    issn = ner.issn
                if ner.isbn:
                    isbn = ner.isbn

        match = score_match(parsed, {
            "title": title,
            "authors": clean_scholar_authors(authors),
            "year": year,
            "doi": doi,
            "url": cand.url,
            "journal": journal,
            "database": "Google Scholar",
            "search_url": search_url,
            "volume": volume,
            "issue": issue,
            "pages": pages,
            "publisher": publisher,
            "issn": issn,
            "isbn": isbn,
        })
        scholar_matches.append(match)

    # Merge into existing results
    existing = verify_results.get(request.pdf_id, {}).get(request.source_id)
    if existing is None:
        return {"updated": False, "result": None}

    # Always record that Google Scholar was searched, even if it returned
    # nothing — users need to see the GS link under DATABASE RESULTS.
    if "Google Scholar" not in existing.databases_searched:
        existing.databases_searched.append("Google Scholar")

    if scholar_matches:
        # Keep only the best Scholar candidate, mirroring how every other
        # verifier returns a single MatchResult. Replace any prior Scholar
        # entries so re-runs (e.g. CAPTCHA retry) don't duplicate.
        best_scholar = max(scholar_matches, key=lambda m: m.raw_score)
        existing.all_results = [m for m in existing.all_results if m.database != "Google Scholar"]
        existing.all_results.append(best_scholar)
        if existing.best_match is None or best_scholar.raw_score > existing.best_match.raw_score:
            existing.best_match = best_scholar
        existing.all_results.sort(key=lambda m: m.raw_score, reverse=True)

    # Re-determine status with potentially new best match
    status, problem_tags = determine_verification_status(
        parsed, existing.best_match, existing.url_liveness
    )
    decision_tag = classify_decision(parsed, existing.best_match)
    existing.status = status
    existing.problem_tags = problem_tags
    existing.decision_tag = decision_tag

    # Persist and broadcast
    save_verify_cache(request.pdf_id, verify_results[request.pdf_id])

    await manager.broadcast("verify_source_done", {
        "pdf_id": request.pdf_id,
        "source_id": request.source_id,
        "status": status,
        "problem_tags": problem_tags,
        "decision_tag": decision_tag,
        "decision_tag_override": existing.decision_tag_override,
        "tag_overrides": existing.tag_overrides,
        "url_liveness": existing.url_liveness,
        "best_match": existing.best_match.model_dump() if existing.best_match else None,
        "all_results": [m.model_dump() for m in existing.all_results],
        "databases_searched": list(existing.databases_searched),
        "scholar_url": existing.scholar_url,
        "google_url": existing.google_url,
    })

    # Recalculate and broadcast PDF counts
    results = verify_results[request.pdf_id]
    high = sum(1 for r in results.values() if r.status == "high")
    medium = sum(1 for r in results.values() if r.status == "medium")
    low = sum(1 for r in results.values() if r.status == "low")

    await manager.broadcast("verify_pdf_updated", {
        "pdf_id": request.pdf_id,
        "high": high,
        "medium": medium,
        "low": low,
    })

    return {
        "updated": True,
        "result": existing.model_dump(),
    }
