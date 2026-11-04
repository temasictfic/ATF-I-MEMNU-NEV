"""Regression tests for backend fixes made during the Electron -> Python port."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Keep the tests away from the real user data dir.
os.environ.setdefault("ATFI_OUTPUT_DIR", tempfile.mkdtemp(prefix="atfi-test-"))
os.environ.setdefault("ATFI_NER_ENABLED", "false")


# --------------------------------------------------------------------------
# Atomic writes
# --------------------------------------------------------------------------


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    from utils.atomic_io import atomic_write_text

    target = tmp_path / "sub" / "settings.json"
    atomic_write_text(target, '{"a": 1}')
    atomic_write_text(target, '{"a": 2}')
    assert target.read_text(encoding="utf-8") == '{"a": 2}'
    assert [p.name for p in target.parent.iterdir()] == ["settings.json"]


def test_atomic_write_failure_keeps_old_file(tmp_path: Path, monkeypatch) -> None:
    from utils import atomic_io

    target = tmp_path / "settings.json"
    target.write_text("old", encoding="utf-8")

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", boom)
    with pytest.raises(OSError):
        atomic_io.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]


# --------------------------------------------------------------------------
# Rate limiter
# --------------------------------------------------------------------------


def test_cancelled_waiter_does_not_consume_a_slot() -> None:
    from scrapers.rate_limiter import RateLimiter

    async def scenario() -> float:
        rl = RateLimiter()
        loop = asyncio.get_running_loop()
        await rl.acquire("x", rate=0.2)  # first request goes out immediately
        # Five waiters time out while queued (as per-DB wait_for timeouts do).
        for _ in range(5):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(rl.acquire("x", rate=0.2), timeout=0.01)
        start = loop.time()
        await asyncio.sleep(0.25)  # pacing window has elapsed by now
        await rl.acquire("x", rate=0.2)
        return loop.time() - start

    elapsed = asyncio.run(scenario())
    # Old implementation: the five cancelled waiters had pushed the next
    # slot ~1 s into the future. Now the next acquire is immediate.
    assert elapsed < 0.4


def test_pacing_still_spaces_requests() -> None:
    from scrapers.rate_limiter import RateLimiter

    async def scenario() -> float:
        rl = RateLimiter()
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(*(rl.acquire("y", rate=0.1) for _ in range(4)))
        return loop.time() - start

    assert asyncio.run(scenario()) >= 0.29


def test_reregistering_same_window_keeps_history() -> None:
    from scrapers.rate_limiter import RateLimiter

    rl = RateLimiter()
    rl.register_window("api.openaire.eu", 2, 3600)
    assert rl.consume_window("api.openaire.eu")
    assert rl.consume_window("api.openaire.eu")
    # The OpenAIRE token manager re-asserts the anon cap on every call.
    rl.register_window("api.openaire.eu", 2, 3600)
    assert not rl.consume_window("api.openaire.eu"), "cap must survive re-registration"
    # A *different* cap (auth state change) still resets the window.
    rl.register_window("api.openaire.eu", 7200, 3600)
    assert rl.consume_window("api.openaire.eu")


# --------------------------------------------------------------------------
# Verifier helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Öztürk, A.", "Öztürk"),
        ("Şahin, M. K.", "Şahin"),
        ("A. Bajaj", "Bajaj"),
        ("Ç. Güneş", "Güneş"),
        ("Smith, J.", "Smith"),
        ("O'Neil, P.", "O'Neil"),
    ],
)
def test_crossref_family_name_keeps_non_ascii(raw: str, expected: str) -> None:
    from verifiers.crossref import _extract_family_name

    assert _extract_family_name(raw) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2021, 2021), ("2021", 2021), ("2019-05", 2019), (None, None), ("n.d.", None), (True, None)],
)
def test_trdizin_year_is_coerced(value, expected) -> None:
    from verifiers.trdizin import _coerce_year

    assert _coerce_year(value) == expected


# --------------------------------------------------------------------------
# Loopback guard + same-origin UI serving
# --------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path):
    from fastapi.testclient import TestClient
    from main import create_app

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><html><head><title>x</title></head><body></body></html>", encoding="utf-8"
    )
    (dist / "app.js").write_text("console.log(1)", encoding="utf-8")
    app = create_app(
        token="s3cret",
        frontend_dir=dist,
        boot_info=lambda: {"port": 1, "token": "s3cret", "note": "</script><b>"},
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


def test_api_requires_token(client) -> None:
    assert client.get("/api/settings").status_code == 401
    assert client.get("/api/settings", headers={"X-Atfi-Token": "wrong"}).status_code == 401
    ok = client.get("/api/settings", headers={"X-Atfi-Token": "s3cret"})
    assert ok.status_code == 200
    assert "api_keys" in ok.json()


def test_health_is_public(client) -> None:
    assert client.get("/api/health").status_code == 200


def test_foreign_host_header_is_rejected(client) -> None:
    # DNS-rebinding: attacker.example resolving to 127.0.0.1
    r = client.get("/api/settings", headers={"X-Atfi-Token": "s3cret", "Host": "attacker.example:8765"})
    assert r.status_code == 400


def test_no_wildcard_cors(client) -> None:
    r = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_index_injects_boot_info_safely(client) -> None:
    r = client.get("/")
    assert r.status_code == 200
    body = r.text
    assert "window.__ATFI__" in body
    assert "</script><b>" not in body  # cannot break out of the script tag
    assert r.headers["x-frame-options"] == "DENY"
    assert "no-store" in r.headers["cache-control"]
    assert client.get("/app.js").text == "console.log(1)"


def test_websocket_requires_token_and_origin(client) -> None:
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("ws://127.0.0.1:8765/api/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "ws://127.0.0.1:8765/api/ws?token=s3cret", headers={"Origin": "https://evil.example"}
        ):
            pass
    with client.websocket_connect(
        "ws://127.0.0.1:8765/api/ws?token=s3cret", headers={"Origin": "http://127.0.0.1:8765"}
    ):
        pass


def test_secrets_are_redacted_from_logs() -> None:
    from api.websocket import redact_secrets

    line = redact_secrets("GET https://x/works?search=a&api_key=SECRET&per_page=5 refreshToken=abc")
    assert "SECRET" not in line and "abc" not in line
    assert "per_page=5" in line
