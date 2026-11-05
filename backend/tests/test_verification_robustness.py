"""Verification robustness: internal errors, cancellation, races, pacing."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("ATFI_OUTPUT_DIR", tempfile.mkdtemp(prefix="atfi-test-"))
os.environ.setdefault("ATFI_NER_ENABLED", "false")

from models.source import BoundingBox, SourceRectangle  # noqa: E402
from models.verification_result import VerificationResult  # noqa: E402
from services import verification_orchestrator as orch  # noqa: E402


def _source(sid: str, pdf: str = "a" * 16) -> SourceRectangle:
    return SourceRectangle(
        id=sid, pdf_id=pdf, bbox=BoundingBox(x0=0, y0=0, x1=1, y1=1, page=0),
        text=f"Author, A. (2020). Title {sid}. Journal.", status="approved",
    )


@pytest.fixture()
def events(monkeypatch):
    sent: list[tuple[str, dict[str, Any]]] = []

    async def broadcast(event_type, data):
        sent.append((event_type, data))

    async def send_log(*args, **kwargs):
        pass

    monkeypatch.setattr(orch.manager, "broadcast", broadcast)
    monkeypatch.setattr(orch.manager, "send_log", send_log)
    monkeypatch.setattr(orch, "save_verify_cache", lambda *a, **k: None)
    return sent


# --- item 1: an internal error is not a "fabricated" verdict -----------------


def test_internal_error_restores_previous_result(monkeypatch, events) -> None:
    pdf = "b" * 16

    async def boom(text):
        raise ValueError("unexpected NER output")

    monkeypatch.setattr(orch, "extract_source_fields", boom)
    previous = VerificationResult(source_id="s1", status="high", decision_tag="valid")
    store = {pdf: {"s1": previous}}
    asyncio.run(orch._verify_source(pdf, _source("s1", pdf), store, 0, "job1"))

    assert store[pdf]["s1"] is previous  # kept, not overwritten with low/fabricated
    failed = [d for t, d in events if t == "verify_source_failed"]
    assert len(failed) == 1 and "unexpected NER output" in failed[0]["error"]
    assert failed[0]["restored"]["status"] == "high"
    assert not [d for t, d in events if t == "verify_source_done"]


def test_internal_error_on_first_run_leaves_source_unverified(monkeypatch, events) -> None:
    pdf = "c" * 16

    async def boom(text):
        raise RuntimeError("bug")

    monkeypatch.setattr(orch, "extract_source_fields", boom)
    store: dict[str, dict[str, VerificationResult]] = {pdf: {}}
    asyncio.run(orch._verify_source(pdf, _source("s1", pdf), store, 0, "job1"))
    assert "s1" not in store[pdf]
    assert [d["restored"] for t, d in events if t == "verify_source_failed"] == [None]


# --- item 2: "cancel all" stops a multi-PDF batch ----------------------------


def test_cancel_all_stops_batch_before_next_pdf(monkeypatch) -> None:
    from api import verification as api_verification
    from services.job_store import verify_jobs

    processed: list[str] = []

    async def fake_verify(pid, sources, *args, **kwargs):
        processed.append(pid)
        orch.cancel_all_active()  # user presses "Cancel all" during the first PDF

    async def send_log(*args, **kwargs):
        pass

    monkeypatch.setattr(orch, "verify_pdf_sources_filtered", fake_verify)
    monkeypatch.setattr(api_verification, "_load_sources_or_empty", lambda pid: [_source("x", pid)])
    monkeypatch.setattr(api_verification, "save_verify_cache", lambda *a, **k: None)
    monkeypatch.setattr(api_verification.manager, "send_log", send_log)
    verify_jobs["jobB"] = {"status": "running", "pdfs": ["p1", "p2", "p3"]}
    asyncio.run(api_verification._verify_batch("jobB", ["p1", "p2", "p3"], {}, set()))
    assert processed == ["p1"]
    assert verify_jobs["jobB"]["status"] == "cancelled"
    assert not orch.batch_cancelled("jobB")  # flags are cleaned up


# --- item 3: per-source Stop and source edits during a PDF run ---------------


def _patch_slow_verifier(monkeypatch, started: list[str]):
    """Replace the network part with a slow fake that settles every source as high."""

    async def fake_verify_source(pdf_id, source, results_store, index=0, job_id=""):
        key = (pdf_id, source.id)
        orch._active_source_jobs[key] = job_id
        results_store[pdf_id][source.id] = VerificationResult(source_id=source.id, status="in_progress")
        started.append(source.id)
        cancelled = False
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if key in orch._retired_sources:
                orch._retired_sources.discard(key)
                results_store[pdf_id].pop(source.id, None)
            elif cancelled:
                results_store[pdf_id].pop(source.id, None)
            else:
                results_store[pdf_id][source.id] = VerificationResult(source_id=source.id, status="high")
            orch._active_source_jobs.pop(key, None)

    monkeypatch.setattr(orch, "_verify_source", fake_verify_source)


def test_per_source_stop_reaches_pdf_run(monkeypatch, events) -> None:
    started: list[str] = []
    _patch_slow_verifier(monkeypatch, started)
    pdf = "d" * 16
    store: dict[str, dict[str, VerificationResult]] = {}

    async def scenario():
        run = asyncio.create_task(orch.verify_pdf_sources(pdf, [_source("s1", pdf), _source("s2", pdf)], store))
        await asyncio.sleep(0.05)
        orch.request_cancel("s1")  # what POST /verify/cancel/source/s1 does
        await run

    asyncio.run(scenario())
    assert "s1" not in store[pdf]  # stopped
    assert store[pdf]["s2"].status == "high"


def test_removed_source_does_not_come_back(monkeypatch, events) -> None:
    started: list[str] = []
    _patch_slow_verifier(monkeypatch, started)
    monkeypatch.setattr(orch, "get_max_concurrent_sources_per_pdf", lambda: 1)  # s2 waits in the queue
    pdf = "e" * 16
    store: dict[str, dict[str, VerificationResult]] = {}

    async def scenario():
        run = asyncio.create_task(
            orch.verify_pdf_sources(pdf, [_source("s1", pdf), _source("s2", pdf), _source("s3", pdf)], store)
        )
        await asyncio.sleep(0.05)
        # The user deletes s1 (running) and s2 (still queued) from the source list.
        removed = orch.retire_sources(pdf, {"s3"}, set(store[pdf]))
        for key in removed:
            store[pdf].pop(key, None)
        await run
        return removed

    removed = asyncio.run(scenario())
    assert removed == {"s1", "s2"}
    assert set(store[pdf]) == {"s3"}
    assert "s2" not in started  # the queued one never ran
    assert not orch._retired_sources and pdf not in orch._pdf_run_sources


# --- item 4: Europe PMC and Open Library are paced ----------------------------


@pytest.mark.parametrize(
    ("module", "host"), [("verifiers.europe_pmc", "www.ebi.ac.uk"), ("verifiers.open_library", "openlibrary.org")]
)
def test_verifier_acquires_pacing_slot(monkeypatch, module, host) -> None:
    import importlib

    mod = importlib.import_module(module)
    hosts: list[str] = []

    class Stop(Exception):
        pass

    async def fake_acquire(h, max_wait):
        hosts.append(h)
        raise Stop()

    monkeypatch.setattr(mod, "acquire_or_rate_limited", fake_acquire)
    with pytest.raises(Stop):
        if module.endswith("europe_pmc"):
            asyncio.run(mod._search_query(None, 'TITLE:"x"', None))
        else:
            asyncio.run(mod._search_query(None, {"title": "x"}, None))
    assert hosts == [host]


def test_real_verify_source_drops_result_of_retired_source(monkeypatch, events) -> None:
    pdf = "f" * 16

    async def slow_parse(text):
        await asyncio.sleep(1)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(orch, "extract_source_fields", slow_parse)
    previous = VerificationResult(source_id="s1", status="medium")
    store = {pdf: {"s1": previous}}

    async def scenario():
        run = asyncio.create_task(orch.verify_pdf_sources(pdf, [_source("s1", pdf)], store, job_id="j"))
        await asyncio.sleep(0.05)
        orch.retire_sources(pdf, set(), set(store[pdf]))  # s1 deleted while running
        await run

    asyncio.run(scenario())
    # Neither finalized nor restored to the previous (medium) result.
    assert "s1" not in store[pdf]
    assert not orch._retired_sources
