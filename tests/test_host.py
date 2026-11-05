"""Tests for the desktop host logic that doesn't need Windows/WebView2."""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


class RecordingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []

    def publish(self, channel: str, payload: Any = None) -> None:
        self.items.append((channel, payload))

    def channels(self) -> list[str]:
        return [c for c, _ in self.items]


# --------------------------------------------------------------------------
# Tiny HTTP server with Range support (GitHub API / release assets stand-in)
# --------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    routes: dict[str, bytes] = {}
    hits: list[tuple[str, str | None]] = []

    def log_message(self, *args: Any) -> None:  # silence
        pass

    def do_GET(self) -> None:  # noqa: N802
        body = self.routes.get(self.path)
        self.hits.append((self.path, self.headers.get("Range")))
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            chunk = body[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
            self.end_headers()
            self.wfile.write(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def server():
    _Handler.routes = {}
    _Handler.hits = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


# --------------------------------------------------------------------------
# Updater
# --------------------------------------------------------------------------


def test_version_compare() -> None:
    from atfi_app.updater import is_newer

    assert is_newer("3.0.1", "3.0.0")
    assert is_newer("v3.1", "3.0.9")
    assert not is_newer("3.0.0", "3.0.0")
    assert not is_newer("2.17.1", "3.0.0")
    assert not is_newer("garbage", "3.0.0")


def _release(base: str, version: str, installer: bytes, *, good_sum: bool = True) -> dict[str, bytes]:
    name = f"Kaynakca-Kontrol-Sistemi-Setup-{version}.exe"
    digest = hashlib.sha256(installer if good_sum else b"other").hexdigest()
    release = {
        "tag_name": f"v{version}",
        "body": "notes",
        "assets": [
            {"name": name, "browser_download_url": f"{base}/dl/{name}"},
            {"name": "SHA256SUMS", "browser_download_url": f"{base}/dl/SHA256SUMS"},
        ],
    }
    return {
        "/repos/x/y/releases/latest": json.dumps(release).encode(),
        f"/dl/{name}": installer,
        "/dl/SHA256SUMS": f"{digest}  {name}\n".encode(),
    }


def _updater(tmp_path: Path, base: str, events: RecordingEvents, current: str = "3.0.0"):
    from atfi_app.updater import Updater

    return Updater(
        events,  # type: ignore[arg-type]
        tmp_path / "updates",
        enabled=True,
        repo="x/y",
        current_version=current,
        api_url_template=base + "/repos/{repo}/releases/latest",
    )


def test_update_available_download_and_verify(tmp_path: Path, server) -> None:
    _, base = server
    installer = os.urandom(300_000)
    _Handler.routes = _release(base, "3.1.0", installer)
    events = RecordingEvents()
    up = _updater(tmp_path, base, events)

    assert up.check() is True
    assert events.items[-1] == ("update:available", {"version": "3.1.0", "releaseNotes": "notes"})
    assert up.state()["stage"] == "available"

    up.download()
    for _ in range(100):
        if up.state()["stage"] == "ready":
            break
        time.sleep(0.05)
    assert up.state()["stage"] == "ready"
    assert "update:downloaded" in events.channels()
    assert any(c == "update:progress" for c in events.channels())
    assert up.installer_path().read_bytes() == installer


def test_update_checksum_mismatch_is_reported(tmp_path: Path, server) -> None:
    _, base = server
    _Handler.routes = _release(base, "3.1.0", os.urandom(1000), good_sum=False)
    events = RecordingEvents()
    up = _updater(tmp_path, base, events)
    assert up.check()
    up.download()
    for _ in range(100):
        if up.state()["stage"] == "error":
            break
        time.sleep(0.05)
    assert up.state()["stage"] == "error"
    assert "update:error" in events.channels()  # user-initiated: always surfaced
    assert up.installer_path() is None


def test_no_update_when_current(tmp_path: Path, server) -> None:
    _, base = server
    _Handler.routes = _release(base, "3.0.0", b"x")
    events = RecordingEvents()
    up = _updater(tmp_path, base, events)
    assert up.check() is False
    assert events.channels() == ["update:notAvailable"]


def test_cancel_disables_install_on_quit(tmp_path: Path, server) -> None:
    _, base = server
    events = RecordingEvents()
    up = _updater(tmp_path, base, events)
    up.cancel()
    assert up.install_on_quit is False


# --------------------------------------------------------------------------
# Model manager
# --------------------------------------------------------------------------


def _manifest(base: str, files: dict[str, bytes], version: str = "v1") -> dict[str, Any]:
    return {
        "name": "m",
        "version": version,
        "files": [
            {
                "name": n,
                "size": len(b),
                "sha256": hashlib.sha256(b).hexdigest(),
                "urls": [f"{base}/missing/{n}", f"{base}/model/{n}"],
            }
            for n, b in files.items()
        ],
    }


def test_model_download_with_mirror_fallback_and_marker(tmp_path: Path, server) -> None:
    from atfi_app.model_manager import ModelManager

    _, base = server
    files = {"model.onnx": os.urandom(500_000), "config.json": b'{"id2label": {"0": "O"}}'}
    _Handler.routes = {f"/model/{n}": b for n, b in files.items()}
    events = RecordingEvents()
    mm = ModelManager(events, tmp_path / "models", manifest=_manifest(base, files), legacy_dirs=[])  # type: ignore[arg-type]
    assert mm.ready_path() is None

    ready: list[Path] = []
    mm.start(on_ready=ready.append)
    mm._thread.join(10)  # noqa: SLF001
    assert mm.status() == {"state": "ready"}
    assert ready == [mm.model_dir]
    assert mm.ready_path() == mm.model_dir
    assert (mm.model_dir / "model.onnx").read_bytes() == files["model.onnx"]
    states = [p["state"] for c, p in events.items if c == "model:status"]
    assert "downloading" in states and states[-1] == "ready"


def test_model_resume_partial_download(tmp_path: Path, server) -> None:
    from atfi_app.model_manager import ModelManager

    _, base = server
    blob = os.urandom(400_000)
    files = {"model.onnx": blob}
    manifest = _manifest(base, files)
    manifest["files"][0]["urls"] = [f"{base}/model/model.onnx"]
    _Handler.routes = {"/model/model.onnx": blob}
    mm = ModelManager(RecordingEvents(), tmp_path / "models", manifest=manifest, legacy_dirs=[])  # type: ignore[arg-type]
    mm.model_dir.mkdir(parents=True)
    (mm.model_dir / "model.onnx.part").write_bytes(blob[:150_000])
    mm.start(on_ready=lambda p: None)
    mm._thread.join(10)  # noqa: SLF001
    assert mm.status()["state"] == "ready"
    assert ("/model/model.onnx", "bytes=150000-") in _Handler.hits


def test_model_adopts_legacy_electron_copy(tmp_path: Path, server) -> None:
    from atfi_app.model_manager import ModelManager

    _, base = server
    files = {"model.onnx": os.urandom(1000)}
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "model.onnx").write_bytes(files["model.onnx"])
    mm = ModelManager(RecordingEvents(), tmp_path / "models", manifest=_manifest(base, files), legacy_dirs=[legacy])  # type: ignore[arg-type]
    mm.start(on_ready=lambda p: None)
    mm._thread.join(10)  # noqa: SLF001
    assert mm.status()["state"] == "ready"
    assert _Handler.hits == []  # nothing downloaded


def test_model_bad_hash_is_an_error(tmp_path: Path, server) -> None:
    from atfi_app.model_manager import ModelManager

    _, base = server
    files = {"model.onnx": b"expected"}
    _Handler.routes = {"/model/model.onnx": b"tampered"}
    manifest = _manifest(base, files)
    manifest["files"][0]["size"] = len(b"tampered")
    mm = ModelManager(RecordingEvents(), tmp_path / "models", manifest=manifest, legacy_dirs=[])  # type: ignore[arg-type]
    mm.start(on_ready=lambda p: None)
    mm._thread.join(10)  # noqa: SLF001
    assert mm.status()["state"] == "error"
    assert not (mm.model_dir / "model.onnx").exists()


def test_bundled_manifest_is_consistent() -> None:
    from atfi_app.model_manager import load_manifest

    m = load_manifest()
    names = {f["name"] for f in m["files"]}
    assert {"config.json", "tokenizer.json"} <= names
    assert any(n.endswith(".onnx") for n in names)
    for f in m["files"]:
        assert len(f["sha256"]) == 64 and f["size"] > 0 and f["urls"]


# --------------------------------------------------------------------------
# Session store
# --------------------------------------------------------------------------


def test_session_rotation_keeps_last_list_across_empty_runs(tmp_path: Path) -> None:
    from atfi_app.session_store import SessionStore

    f = tmp_path / "s.json"
    s = SessionStore(f)
    assert s.rotate() == []
    s.save_current([{"path": "C:/a.pdf", "name": "a.pdf", "importedAt": 1}])
    assert [e["path"] for e in SessionStore(f).rotate()] == ["C:/a.pdf"]
    # A run without imports must not wipe the list (it used to).
    assert [e["path"] for e in SessionStore(f).rotate()] == ["C:/a.pdf"]
    s2 = SessionStore(f)
    s2.rotate()
    s2.save_current([{"path": "C:/b.pdf"}, {"bad": 1}, "x"])
    assert [e["path"] for e in SessionStore(f).rotate()] == ["C:/b.pdf"]


# --------------------------------------------------------------------------
# Host API (router + services) through the real backend app
# --------------------------------------------------------------------------


@pytest.fixture()
def host_client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ATFI_USER_DATA_DIR", str(tmp_path / "ud"))
    monkeypatch.setenv("ATFI_LOCAL_DATA_DIR", str(tmp_path / "ld"))
    monkeypatch.setenv("ATFI_DEV_PICK_PDFS", os.pathsep.join(["/x/a.pdf", "/x/b.pdf"]))
    from fastapi.testclient import TestClient

    from atfi_app.host_api import HostServices, build_router
    from atfi_app.model_manager import ModelManager
    from atfi_app.platform_base import DevBrowserPlatform
    from atfi_app.session_store import SessionStore
    from atfi_app.updater import Updater
    from main import create_app

    events = RecordingEvents()
    services = HostServices(
        DevBrowserPlatform(),
        session=SessionStore(tmp_path / "s.json"),
        updater=Updater(events, tmp_path / "u", enabled=False),  # type: ignore[arg-type]
        model=ModelManager(events, tmp_path / "m", legacy_dirs=[]),  # type: ignore[arg-type]
    )
    app = create_app(token="t", extra_routers=[build_router(services)])
    with TestClient(app, base_url="http://127.0.0.1:9000") as client:
        yield client, services


def _rpc(client, method: str, *args: Any, token: str = "t"):
    return client.post(f"/__host/rpc/{method}", json={"args": list(args)}, headers={"X-Atfi-Token": token})


def test_host_rpc_requires_token(host_client) -> None:
    client, _ = host_client
    assert _rpc(client, "selectPdfs", token="wrong").status_code == 401
    assert client.get("/__host/pdf", params={"path": "/x.pdf"}).status_code == 401


def test_host_rpc_dialog_and_unknown_method(host_client) -> None:
    client, _ = host_client
    assert _rpc(client, "selectPdfs").json() == {"result": ["/x/a.pdf", "/x/b.pdf"]}
    assert _rpc(client, "noSuchThing").status_code == 404


def test_host_list_pdfs_sorted_like_locale_compare(host_client, tmp_path: Path) -> None:
    client, _ = host_client
    d = tmp_path / "pdfs"
    d.mkdir()
    for name in ["b.pdf", "Ä.pdf", "a.PDF", "c.txt", "Z.pdf"]:
        (d / name).write_bytes(b"%PDF")
    (d / "sub.pdf").mkdir()
    names = [Path(p).name for p in _rpc(client, "listPdfsInDirectory", str(d)).json()["result"]]
    assert names == ["a.PDF", "Ä.pdf", "b.pdf", "Z.pdf"]
    assert _rpc(client, "listPdfsInDirectory", str(d / "missing")).json()["result"] == []


def test_host_pdf_read_write_roundtrip(host_client, tmp_path: Path) -> None:
    client, _ = host_client
    target = tmp_path / "out.pdf"
    payload = b"%PDF-1.7\n" + os.urandom(200_000)
    r = client.put("/__host/pdf", params={"path": str(target)}, content=payload, headers={"X-Atfi-Token": "t"})
    assert r.status_code == 200
    assert target.read_bytes() == payload
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []  # no temp left
    r = client.get("/__host/pdf", params={"path": str(target)}, headers={"X-Atfi-Token": "t"})
    assert r.status_code == 200 and r.content == payload


def test_host_pdf_rejects_non_pdf_and_relative(host_client, tmp_path: Path) -> None:
    client, _ = host_client
    h = {"X-Atfi-Token": "t"}
    assert client.get("/__host/pdf", params={"path": str(tmp_path / "x.txt")}, headers=h).status_code == 400
    assert client.get("/__host/pdf", params={"path": "rel.pdf"}, headers=h).status_code == 400
    assert client.put("/__host/pdf", params={"path": str(tmp_path / "evil.exe")}, content=b"x", headers=h).status_code == 400


def test_open_external_only_allows_web_links(host_client, monkeypatch) -> None:
    client, _ = host_client
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    assert _rpc(client, "openExternal", "https://example.org").status_code == 200
    assert _rpc(client, "openExternal", "file:///C:/Windows/System32/calc.exe").status_code == 400
    assert _rpc(client, "openExternal", "ms-settings:privacy").status_code == 400
    assert opened == ["https://example.org"]


def test_exclusion_list_fetch_and_offline_cache(host_client, server, monkeypatch) -> None:
    client, _ = host_client
    _, base = server
    _Handler.routes = {"/list.tsv": "a\tb\n".encode()}
    first = _rpc(client, "fetchExclusionList", f"{base}/list.tsv").json()["result"]
    assert first == {"text": "a\tb\n", "fromCache": False}
    offline = _rpc(client, "fetchExclusionList", f"{base}/gone.tsv").json()["result"]
    assert offline["fromCache"] is True and offline["text"] == "a\tb\n"


# --------------------------------------------------------------------------
# Backend host: real uvicorn on a pre-bound socket, graceful stop
# --------------------------------------------------------------------------


def test_backend_host_serves_ui_and_stops(tmp_path: Path, monkeypatch) -> None:
    import urllib.request

    from atfi_app.backend_host import BackendHost, prepare_backend_import

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html><head></head><body>ok</body></html>", encoding="utf-8")
    prepare_backend_import(ROOT / "backend", output_dir=tmp_path / "out", model_dir=None)
    loops: list[Any] = []
    host = BackendHost(
        frontend_dir=dist,
        boot_info=lambda: {"port": host.port, "token": host.token},
        extra_routers=[],
        on_loop_ready=loops.append,
    )
    host.start()
    host.wait_started(timeout=30)
    try:
        html = urllib.request.urlopen(host.url, timeout=5).read().decode()  # noqa: S310
        assert f'"token": "{host.token}"' in html
        assert loops, "lifespan must hand the loop to the host"
        req = urllib.request.Request(host.url + "api/settings", headers={"X-Atfi-Token": host.token})
        assert urllib.request.urlopen(req, timeout=5).status == 200  # noqa: S310
    finally:
        host.stop()
    assert not host._thread.is_alive()  # noqa: SLF001


# --------------------------------------------------------------------------
# Single instance + drop queue
# --------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX path variant")
def test_single_instance_activation(tmp_path: Path) -> None:
    from atfi_app.single_instance import SingleInstance

    first = SingleInstance(tmp_path)
    assert first.notify_existing() is False
    activated = threading.Event()
    first.listen(activated.set)
    try:
        assert SingleInstance(tmp_path).notify_existing() is True
        assert activated.wait(5)
    finally:
        first.close()


def test_drop_queue_waits_for_paths() -> None:
    from atfi_app.native.platform import _DropQueue

    q = _DropQueue()
    threading.Timer(0.1, lambda: q.push(["C:/a.pdf", "C:/b.pdf"])).start()
    assert q.take(2, timeout=2) == ["C:/a.pdf", "C:/b.pdf"]
    assert q.take(1, timeout=0.05) == []
