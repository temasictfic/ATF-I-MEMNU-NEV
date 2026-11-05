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
    async def no_save(*a, **k):
        pass

    monkeypatch.setattr(orch, "save_verify_cache_async", no_save)
    return sent


# --- item 1: an internal error is not a "fabricated" verdict -----------------


def test_internal_error_restores_previous_result(monkeypatch, events) -> None:
    pdf = "b" * 16

    async def boom(text, **kwargs):
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

    async def boom(text, **kwargs):
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
    async def load(pid):
        return [_source("x", pid)]

    async def no_save(*a, **k):
        pass

    monkeypatch.setattr(api_verification, "_load_sources_or_empty", load)
    monkeypatch.setattr(api_verification, "save_verify_cache_async", no_save)
    monkeypatch.setattr(api_verification.manager, "send_log", send_log)
    verify_jobs["jobB"] = {"status": "running", "pdfs": ["p1", "p2", "p3"]}
    asyncio.run(api_verification._verify_batch("jobB", ["p1", "p2", "p3"], {}, set()))
    assert processed == ["p1"]
    assert verify_jobs["jobB"]["status"] == "cancelled"
    assert not orch.batch_cancelled("jobB")  # flags are cleaned up


# --- item 3: per-source Stop and source edits during a PDF run ---------------


def _patch_slow_verifier(monkeypatch, started: list[str]):
    """Replace the network part with a slow fake that settles every source as high."""

    async def fake_verify_source(pdf_id, source, results_store, index=0, job_id="", **kwargs):
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

    async def slow_parse(text, **kwargs):
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


# --- item 6: source text is cleaned exactly once --------------------------------


def test_ui_text_is_not_cleaned_again(monkeypatch, events) -> None:
    """'[12] 3-D printing' is shown as '3-D printing'; it must be verified as that."""
    seen: list[str] = []

    async def capture_parse(text, *, already_clean=False):
        seen.append(text if already_clean else f"RAW:{text}")
        raise RuntimeError("stop here")

    monkeypatch.setattr(orch, "extract_source_fields", capture_parse)
    pdf = "g" * 16
    store: dict[str, dict[str, VerificationResult]] = {}
    sources = [_source("s1", pdf), _source("s2", pdf)]
    sources[1] = sources[1].model_copy(update={"text": "[7] 3-D printing of tissue. Nature (2019)."})
    custom = {"s1": "3-D printing in medicine. Lancet (2020)."}  # displayed/edited in the UI
    asyncio.run(orch.verify_pdf_sources_filtered(pdf, sources, store, custom, set(), job_id="j"))
    assert sorted(seen) == [
        "3-D printing in medicine. Lancet (2020).",  # UI text used as-is
        "3-D printing of tissue. Nature (2019).",  # raw cache text cleaned once
    ]


def test_extract_fields_cleans_once() -> None:
    from services.source_extractor import _extract_source_fields_regex
    from utils.text_cleaning import strip_source_noise

    assert strip_source_noise(strip_source_noise("[12] 3-D printing")) == "D printing"  # why it matters
    clean = strip_source_noise("[12] 3-D printing in medicine. Lancet (2020).")
    parsed = _extract_source_fields_regex(clean, already_clean=True)
    assert parsed.raw_text.startswith("3-D")


# --- item 12: DNS rebinding is blocked at connect time ---------------------------


def test_resolver_drops_private_addresses(monkeypatch) -> None:
    from services import url_checker

    async def fake_inner_resolve(self, host, port=0, family=0):
        table = {
            "rebind.example": [{"host": "127.0.0.1"}, {"host": "10.0.0.5"}],
            "mixed.example": [{"host": "10.0.0.5"}, {"host": "93.184.216.34"}],
            "proxy.corp": [{"host": "10.1.2.3"}],
        }
        return [dict(r, hostname=host, port=port, family=family, proto=0, flags=0) for r in table[host]]

    monkeypatch.setattr(url_checker.DefaultResolver, "resolve", fake_inner_resolve)

    async def scenario():
        r = url_checker._PublicOnlyResolver({"proxy.corp"})
        with pytest.raises(OSError):
            await r.resolve("rebind.example", 80)
        mixed = await r.resolve("mixed.example", 80)
        proxy = await r.resolve("proxy.corp", 3128)  # configured proxy stays reachable
        await r.close()
        return [i["host"] for i in mixed], [i["host"] for i in proxy]

    assert asyncio.run(scenario()) == (["93.184.216.34"], ["10.1.2.3"])


# --- item 14: a recorded OpenAIRE failure is cleared when the token changes ------


def test_openaire_failure_status_resets_on_new_token(monkeypatch) -> None:
    from services import openaire_token_manager as otm

    async def fail(token, **kwargs):
        raise RuntimeError("invalid_grant")

    monkeypatch.setattr(otm, "exchange_refresh_token", fail)
    monkeypatch.setattr(otm, "_refresh_window_cap", lambda **k: None)
    assert asyncio.run(otm._refresh_and_cache("old-token")) is None
    status = otm.get_runtime_status()
    assert status["status"] == "failed" and status["last_error"] == "invalid_grant"
    otm.invalidate_cache()  # what connect / disconnect do
    assert otm.get_runtime_status() == {"status": "never", "last_attempt_at_ms": None, "last_error": None}


# --- performance: Crossref variant walk -------------------------------------------


def _crossref_source():
    from models.source import ParsedSource

    return ParsedSource(
        raw_text="Smith, J. (2020). Deep things. Journal of Applied Widgets, 3, 1-2.",
        title="Deep things", authors=["Smith, J."], year=2020,
        journal="Journal of Applied Widgets", parse_confidence=0.9,
    )


@pytest.mark.parametrize(("scores", "expected_calls"), [([0.4] * 8, 5), ([0.4, 1.0, 0.9], 2)])
def test_crossref_variant_walk(monkeypatch, scores, expected_calls) -> None:
    from models.verification_result import MatchResult
    from verifiers import crossref

    calls: list[dict[str, str]] = []

    async def fake_fetch(session, params, source):
        calls.append(params)
        return MatchResult(database="crossref", score=scores[len(calls) - 1])

    monkeypatch.setattr(crossref, "_fetch_best_match", fake_fetch)
    monkeypatch.setattr(crossref, "get_session", lambda: None)
    best = asyncio.run(crossref.search(_crossref_source()))
    assert len(calls) == expected_calls
    assert best is not None and best.score == max(scores[:expected_calls])
    optional = {"query.container-title", "query.author", "filter"}
    assert set(calls[0]) >= optional  # most constrained first
    if expected_calls == 5:
        assert not set(calls[-1]) & optional  # bare title last
        assert len({tuple(sorted(c)) for c in calls}) == 5  # no duplicates


# --- performance: cache and settings I/O off the event loop ----------------------


def test_async_cache_io_round_trip_and_cancel_merge(tmp_path, monkeypatch) -> None:
    from config import settings as app_config
    from services import cache_store

    monkeypatch.setattr(type(app_config), "get_cache_dir", lambda self: tmp_path)
    pdf = "a1" * 8
    disk = {
        "s1": VerificationResult(source_id="s1", status="medium"),
        "s2": VerificationResult(source_id="s2", status="high"),
    }
    live = {
        "s1": VerificationResult(source_id="s1", status="high"),
        "s2": VerificationResult(source_id="s2", status="in_progress"),
    }

    async def scenario():
        # Writes issued back to back land in call order (newest wins).
        await asyncio.gather(*(
            cache_store.save_verify_cache_async(pdf, {"s1": VerificationResult(source_id="s1", status=st)})
            for st in ("low", "low", "medium")
        ))
        assert (await cache_store.load_verify_cache_async(pdf))["s1"].status == "medium"
        await cache_store.save_verify_cache_async(pdf, disk)
        # Cancel path: settled rows win, a running row keeps its disk entry.
        await cache_store.merge_into_verify_cache(pdf, cache_store.snapshot_settled(live))
        return await cache_store.load_verify_cache_async(pdf)

    merged = asyncio.run(scenario())
    assert {k: v.status for k, v in merged.items()} == {"s1": "high", "s2": "high"}


def test_save_settings_async_writes_newest(tmp_path, monkeypatch) -> None:
    import json

    from config import settings as app_config
    from services import settings_store

    path = tmp_path / "settings.json"
    monkeypatch.setattr(type(app_config), "get_settings_path", lambda self: path)
    base = settings_store.get_current_settings()
    monkeypatch.setattr(settings_store, "_current_settings", base)  # restored afterwards

    async def scenario():
        await asyncio.gather(*(
            settings_store.save_settings_async(base.model_copy(update={"search_timeout": t}))
            for t in (11, 12, 13)
        ))

    asyncio.run(scenario())
    assert settings_store.get_current_settings().search_timeout == 13
    assert json.loads(path.read_text(encoding="utf-8"))["search_timeout"] == 13
