"""On-disk cache for parsed sources and verification results.

Single home for the {output_dir}/cache/*.json files and the helpers that
read/write them. Lives under services/ rather than api/ so both
``api.parsing`` and ``api.verification`` (and ``services.verification_orchestrator``)
can import it without forming the parsing↔verification import cycle that
existed when these helpers lived inside ``api.parsing`` / ``api.verification``.

Two cache families:

* ``{pdf_id}.json`` — parsed sources, the ``numbered`` flag, ``approved`` flag,
  and ``page_count`` (added so cached imports can skip a full PDF parse on
  re-open).
* ``verify_{pdf_id}.json`` — per-source verification results in the optimised
  shape produced by :func:`_serialise_result`.
"""

import json
import re
from pathlib import Path

from config import settings
from fastapi import HTTPException
from models.source import SourceRectangle, SourceStatus
from models.verification_result import MatchResult, VerificationResult

from services.search_urls import build_google_urls, build_search_url

# ---------------------------------------------------------------------------
# pdf_id validation (defends every cache-file build site against traversal)
# ---------------------------------------------------------------------------

# Renderer-side IDs are 16-char lowercase hex produced by pdfIdFromPath
# (see lib/pdf/orchestrator.ts). Anything else is refused — there is no
# legacy stem-format to tolerate.
_PDF_ID_RE = re.compile(r"^[a-f0-9]{16}$")


def _validate_pdf_id(pdf_id: str) -> str:
    if not _PDF_ID_RE.match(pdf_id or ""):
        raise HTTPException(status_code=400, detail="Invalid pdf_id")
    return pdf_id


def _safe_cache_path(filename: str) -> Path:
    """Resolve a cache file path and assert it stays directly inside cache_dir.
    The filename is expected to already include a validated pdf_id; this is the
    second line of defence against any traversal that slips past the regex."""
    cache_dir = settings.get_cache_dir().resolve()
    target = (cache_dir / filename).resolve()
    if target.parent != cache_dir:
        raise HTTPException(status_code=400, detail="Invalid pdf_id")
    return target


# ---------------------------------------------------------------------------
# Source cache (`{pdf_id}.json`)
# ---------------------------------------------------------------------------


def _round_bbox(bbox: dict) -> dict:
    """Trim JS-arithmetic float noise on bbox coordinates."""
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in bbox.items()}


def save_sources_cache(
    pdf_id: str,
    sources: list[SourceRectangle],
    numbered: bool = False,
    page_count: int | None = None,
) -> None:
    """Write the parsed-sources JSON for a PDF.

    ``page_count`` is optional for backward compatibility: existing callers
    that don't yet have it pass ``None`` and the field is omitted, in which
    case a subsequent load returns ``None`` for it. New importers in the
    renderer always pass the parsed page count so cached re-opens can skip
    a full PDF parse.
    """
    _validate_pdf_id(pdf_id)
    cache_file = _safe_cache_path(f"{pdf_id}.json")

    approved = bool(sources) and all(s.status == "approved" for s in sources)

    def dump_source(s: SourceRectangle) -> dict:
        d = s.model_dump(exclude={"pdf_id"}, exclude_defaults=True)
        # status defaults to "detected" in the model so exclude_defaults
        # already strips that; also strip "approved" when it matches the
        # whole-PDF state (the common case after the user approves).
        if approved and d.get("status") == "approved":
            d.pop("status", None)
        if "bbox" in d:
            d["bbox"] = _round_bbox(d["bbox"])
        if "bboxes" in d:
            d["bboxes"] = [_round_bbox(b) for b in d["bboxes"]]
        return d

    data: dict = {
        "pdf_id": pdf_id,
        "numbered": numbered,
        "approved": approved,
        "sources": [dump_source(s) for s in sources],
    }
    if page_count is not None:
        data["page_count"] = int(page_count)
    try:
        cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[save_sources_cache] FAILED to write {cache_file}: {e}", flush=True)
        raise


def load_sources_cache(
    pdf_id: str,
) -> tuple[list[SourceRectangle], bool, int | None] | None:
    """Read the parsed-sources JSON for a PDF.

    Returns ``(sources, numbered, page_count)`` or ``None`` if the file is
    missing/unreadable. ``page_count`` is ``None`` when the cache pre-dates
    the page-count addition.
    """
    _validate_pdf_id(pdf_id)
    cache_file = _safe_cache_path(f"{pdf_id}.json")
    if not cache_file.exists():
        return None
    try:
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        approved = bool(raw.get("approved", False))
        default_status = "approved" if approved else "detected"
        sources = [
            SourceRectangle(**{
                **item,
                "pdf_id": pdf_id,
                "status": item.get("status", default_status),
            })
            for item in raw["sources"]
        ]
        page_count_raw = raw.get("page_count")
        page_count: int | None
        try:
            page_count = int(page_count_raw) if page_count_raw is not None else None
        except (TypeError, ValueError):
            page_count = None
        return sources, raw.get("numbered", False), page_count
    except Exception:  # noqa: BLE001
        return None


def load_sources_for_pdf(pdf_id: str) -> list[SourceRectangle] | None:
    """Public helper used by verification.py: read the cached source list."""
    cached = load_sources_cache(pdf_id)
    return None if cached is None else cached[0]


def delete_sources_cache(pdf_id: str) -> None:
    """Drop both the source and verify cache files for a PDF."""
    _validate_pdf_id(pdf_id)
    for name in (f"{pdf_id}.json", f"verify_{pdf_id}.json"):
        cache_file = _safe_cache_path(name)
        if cache_file.exists():
            try:
                cache_file.unlink()
            except Exception as e:  # noqa: BLE001
                print(f"[delete_sources_cache] failed to delete {cache_file}: {e}", flush=True)


def flip_sources_status(pdf_id: str, new_status: SourceStatus) -> bool:
    """Bulk-set ``status`` on every cached source for a PDF and re-persist."""
    cached = load_sources_cache(pdf_id)
    if cached is None:
        return False
    sources, numbered, page_count = cached
    for s in sources:
        s.status = new_status
    save_sources_cache(pdf_id, sources, numbered, page_count)
    return True


# ---------------------------------------------------------------------------
# Verification cache (`verify_{pdf_id}.json`)
# ---------------------------------------------------------------------------


def _is_best_match(m: MatchResult, best: MatchResult | None) -> bool:
    """Identify the all_results entry that should carry best_match=True on disk.

    Matches by (database, score, title) — strong enough to disambiguate
    across the rare ties we see (each verifier returns a single result, so
    the full triple is unique in practice).
    """
    if best is None:
        return False
    return (m.database, m.score, m.title) == (best.database, best.score, best.title)


def _serialise_match(m: MatchResult, is_best: bool) -> dict:
    """Dump a MatchResult for disk: drop search_url + defaults, add best_match flag."""
    d = m.model_dump(exclude={"search_url"}, exclude_defaults=True)
    d["best_match"] = is_best
    return d


def _serialise_result(r: VerificationResult) -> dict:
    """Dump a VerificationResult for disk in the optimised cache shape."""
    payload = r.model_dump(
        exclude={"source_id", "best_match", "all_results", "scholar_url", "google_url"},
        exclude_defaults=True,
    )
    payload["all_results"] = [_serialise_match(m, _is_best_match(m, r.best_match)) for m in r.all_results]
    return payload


def save_verify_cache(pdf_id: str, results: dict[str, VerificationResult]) -> None:
    """Persist verification results to disk cache."""
    _validate_pdf_id(pdf_id)
    cache_file = _safe_cache_path(f"verify_{pdf_id}.json")
    data = {sid: _serialise_result(r) for sid, r in results.items()}
    cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _hydrate_match(entry: dict, parsed_title: str) -> tuple[MatchResult, bool]:
    """Rehydrate a MatchResult from disk; rebuild search_url; return is_best flag."""
    is_best = bool(entry.pop("best_match", False))
    database = entry.get("database", "")
    entry["search_url"] = build_search_url(database, parsed_title)
    return MatchResult(**entry), is_best


def _hydrate_result(source_id: str, payload: dict) -> VerificationResult:
    """Rehydrate a VerificationResult from disk: inject source_id, reconstruct
    best_match from the flagged all_results entry, rebuild search/scholar/google URLs."""
    payload = dict(payload)  # don't mutate caller's data
    parsed_title = payload.get("parsed_title", "") or ""

    raw_all = payload.pop("all_results", [])
    all_results: list[MatchResult] = []
    best_match: MatchResult | None = None
    for entry in raw_all:
        m, is_best = _hydrate_match(dict(entry), parsed_title)
        all_results.append(m)
        if is_best:
            best_match = m

    scholar_url, google_url = build_google_urls(parsed_title)
    return VerificationResult(
        source_id=source_id,
        all_results=all_results,
        best_match=best_match,
        scholar_url=scholar_url,
        google_url=google_url,
        **payload,
    )


def load_verify_cache(pdf_id: str) -> dict[str, VerificationResult] | None:
    """Load verification results from disk cache."""
    _validate_pdf_id(pdf_id)
    cache_file = _safe_cache_path(f"verify_{pdf_id}.json")
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return {sid: _hydrate_result(sid, payload) for sid, payload in data.items()}
    except Exception:  # noqa: BLE001
        return None


def clean_verify_cache(pdf_id: str, current_source_ids: set[str]) -> None:
    """Remove verify cache entries for sources that no longer exist."""
    _validate_pdf_id(pdf_id)
    cache_file = _safe_cache_path(f"verify_{pdf_id}.json")
    if not cache_file.exists():
        return
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        cleaned = {k: v for k, v in data.items() if k in current_source_ids}
        if len(cleaned) != len(data):
            cache_file.write_text(json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001, S110
        pass
