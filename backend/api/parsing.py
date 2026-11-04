"""Sources / cache / NER endpoints.

After Phase 4 of the PDF-handling migration, the backend no longer parses or
renders PDFs. Everything PDF-native (file reads, canvas rendering, source
detection, bbox text extraction, annotation writing) runs in the Electron
renderer. The Python side is now only responsible for:

1. Persisting user-edited source rectangles to a JSON cache on disk
2. Managing approval status on those cached sources
3. Running the NER field extractor (still the only reason Python is in the loop)

Cache I/O lives in :mod:`services.cache_store`; the in-memory verification
state lives in :mod:`services.job_store`. Importing from those neutral
modules instead of cross-importing api.verification breaks the parsing↔
verification cycle that this module used to be half of.
"""

from fastapi import APIRouter, HTTPException
from models.source import SourceRectangle
from pydantic import BaseModel
from services.cache_store import (
    _validate_pdf_id,
    clean_verify_cache,
    delete_sources_cache,
    flip_sources_status,
    load_sources_cache,
    load_sources_for_pdf,  # re-exported for any external callers
    save_sources_cache,
)
from services.job_store import verify_results

from api.websocket import manager

# Backwards-compatible re-export: `load_sources_for_pdf` used to live here and
# was imported from `api.parsing` by other code. Re-exporting keeps that
# import path working while the canonical home is now cache_store.
__all__ = ["load_sources_for_pdf", "router"]

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class UpdateSourcesRequest(BaseModel):
    sources: list[SourceRectangle]
    numbered: bool | None = None
    # Sent by the renderer alongside freshly detected sources so the cache
    # can persist the parsed page count. Cached re-imports then skip the
    # full PDF parse — see lib/pdf/orchestrator.ts. Optional for callers
    # that don't yet have the page count (older clients, edits without a
    # re-parse).
    page_count: int | None = None


class ExtractFieldsRequest(BaseModel):
    text: str


# ---------------------------------------------------------------------------
# Source cache CRUD
# ---------------------------------------------------------------------------


@router.get("/parse/sources/{pdf_id}")
async def get_sources(pdf_id: str):
    # Returns 200 with `cached=false` when no cache entry exists, rather
    # than 404, so the client's orchestrator can do a "check before parse"
    # without producing red network errors in devtools.
    cached = load_sources_cache(pdf_id)
    if cached is None:
        return {"sources": [], "cached": False}
    sources, numbered, page_count = cached
    approved = len(sources) > 0 and all(s.status == "approved" for s in sources)
    payload: dict = {
        "sources": [s.model_dump() for s in sources],
        "cached": True,
        "numbered": numbered,
        "approved": approved,
    }
    if page_count is not None:
        payload["page_count"] = page_count
    return payload


@router.put("/parse/sources/{pdf_id}")
async def update_sources(pdf_id: str, request: UpdateSourcesRequest):
    # Determine the `numbered` flag to persist: explicit from the request
    # wins, then any previous cache entry, else False. Same precedence for
    # `page_count` so plain source-edit saves don't blank out the cached
    # page count.
    prev = load_sources_cache(pdf_id)
    if request.numbered is not None:
        numbered = request.numbered
    else:
        numbered = prev[1] if prev is not None else False

    if request.page_count is not None:
        page_count = request.page_count
    else:
        page_count = prev[2] if prev is not None else None

    save_sources_cache(pdf_id, request.sources, numbered, page_count)

    # Clean stale verify results for removed/renumbered sources
    current_ids = {s.id for s in request.sources}
    clean_verify_cache(pdf_id, current_ids)
    if pdf_id in verify_results:
        verify_results[pdf_id] = {
            k: v for k, v in verify_results[pdf_id].items() if k in current_ids
        }
    return {"success": True}


@router.post("/parse/approve/{pdf_id}")
async def approve_pdf(pdf_id: str):
    if not flip_sources_status(pdf_id, "approved"):
        raise HTTPException(status_code=404, detail="PDF not found")
    await manager.broadcast("parse_approved", {"pdf_id": pdf_id})
    await manager.send_log("success", f"Sources approved for {pdf_id}", pdf_id=pdf_id)
    return {"success": True}


@router.post("/parse/unapprove/{pdf_id}")
async def unapprove_pdf(pdf_id: str):
    if not flip_sources_status(pdf_id, "detected"):
        raise HTTPException(status_code=404, detail="PDF not found")
    await manager.broadcast("parse_unapproved", {"pdf_id": pdf_id})
    await manager.send_log("info", f"Approval revoked for {pdf_id}", pdf_id=pdf_id)
    return {"success": True}


@router.delete("/parse/pdf/{pdf_id}")
async def remove_pdf(pdf_id: str):
    """Drop the cached source + verify JSON files for a PDF."""
    _validate_pdf_id(pdf_id)
    delete_sources_cache(pdf_id)
    verify_results.pop(pdf_id, None)
    return {"success": True}


# ---------------------------------------------------------------------------
# NER field extraction (the one piece that still needs Python)
# ---------------------------------------------------------------------------


@router.post("/parse/extract-fields")
async def extract_fields(request: ExtractFieldsRequest):
    """Extract structured citation fields from raw source text."""
    from services.source_extractor import extract_source_fields

    parsed = await extract_source_fields(request.text)
    return parsed.model_dump()
