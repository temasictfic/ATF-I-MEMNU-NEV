"""HTTP surface of the desktop host (replaces Electron's ipcMain handlers).

Routes are mounted on the in-process backend app and are therefore
same-origin with the UI. The backend's LoopbackGuard requires the per-launch
token on every ``/__host/*`` request (``X-Atfi-Token``).

* ``POST /__host/rpc/{method}`` with body ``{"args": [...]}`` returns
  ``{"result": ...}``.
* ``GET  /__host/pdf?path=...`` reads a ``.pdf`` file (Electron ``pdf:read``).
* ``PUT  /__host/pdf?path=...`` writes a ``.pdf`` atomically (Electron ``pdf:write``).

The PDF bytes are streamed. Electron copied whole files through IPC as a
``Uint8Array``.

Handlers are plain ``def`` functions, so FastAPI runs them in its
threadpool. Blocking work (modal dialogs, network, disk, marshalling to the
WinForms UI thread) never stalls the server's event loop.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import unicodedata
import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from . import net, paths
from .model_manager import ModelManager
from .platform_base import Platform
from .session_store import SessionStore
from .updater import Updater

logger = logging.getLogger("atfi.host")

_EXTERNAL_SCHEMES = {"http", "https", "mailto"}


def _require_pdf_path(raw: str) -> Path:
    if not raw or not isinstance(raw, str):
        raise HTTPException(400, "path is required")
    path = Path(raw)
    if path.suffix.lower() != ".pdf":
        raise HTTPException(400, "only .pdf files are allowed")
    if not path.is_absolute():
        raise HTTPException(400, "path must be absolute")
    return path


def _locale_sort_key(text: str) -> tuple[str, str]:
    # Approximates JS `localeCompare` (Electron sorted with it): accent- and
    # case-insensitive first, then the raw string as a tiebreaker.
    base = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return (base.casefold(), text)


def open_with_system(target: str) -> None:
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", target])  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", target])  # noqa: S603, S607


class HostServices:
    """Implementation of every ``hostAPI`` method in frontend/src/lib/host/bridge.ts."""

    def __init__(
        self,
        platform: Platform,
        *,
        session: SessionStore,
        updater: Updater,
        model: ModelManager,
    ) -> None:
        self.platform = platform
        self.session = session
        self.updater = updater
        self.model = model
        self._methods: dict[str, Callable[..., Any]] = {
            "selectDirectory": self.select_directory,
            "selectPdfs": self.select_pdfs,
            "showSaveAs": self.show_save_as,
            "listPdfsInDirectory": self.list_pdfs_in_directory,
            "takeDroppedPaths": self.take_dropped_paths,
            "openExternal": self.open_external,
            "openCacheFolder": self.open_cache_folder,
            "fetchExclusionList": self.fetch_exclusion_list,
            "openDevTools": self.platform.open_devtools,
            "clearScholarSession": self.clear_scholar_session,
            "saveSessionPdfs": self.session.save_current,
            "downloadUpdate": self.updater.download,
            "cancelUpdate": self.updater.cancel,
            "installUpdate": self.install_update,
            "getUpdateState": self.updater.state,
            "getModelStatus": self.model.status,
            "retryModelDownload": self.model.retry,
            "webview.create": self.platform.webview_create,
            "webview.destroy": self.platform.webview_destroy,
            "webview.setBounds": self.platform.webview_set_bounds,
            "webview.call": self.platform.webview_call,
        }

    def dispatch(self, method: str, args: list[Any]) -> Any:
        fn = self._methods.get(method)
        if fn is None:
            raise HTTPException(404, f"unknown host method {method!r}")
        return fn(*args)

    # ---- dialogs -------------------------------------------------------------

    def select_directory(self, default_path: str | None = None) -> str | None:
        return self.platform.select_directory(default_path or None, "Select PDF Directory")

    def select_pdfs(self) -> list[str]:
        return list(self.platform.select_pdfs("Select PDF Files"))

    def show_save_as(self, options: dict[str, Any] | None = None) -> str | None:
        options = options or {}
        filters = options.get("filters") or [{"name": "PDF Files", "extensions": ["pdf"]}]
        return self.platform.save_as(
            options.get("title") or "Save PDF",
            options.get("defaultPath"),
            filters,
            options.get("buttonLabel"),
        )

    # ---- files ---------------------------------------------------------------

    def list_pdfs_in_directory(self, directory: str) -> list[str]:
        if not isinstance(directory, str) or not directory:
            return []
        base = Path(directory)
        if not base.is_dir():
            return []
        found = [str(p) for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
        return sorted(found, key=_locale_sort_key)

    def take_dropped_paths(self, count: int) -> list[str]:
        count = max(0, min(int(count), 1000))
        return self.platform.take_dropped_paths(count, timeout=3.0)

    # ---- shell ---------------------------------------------------------------

    def open_external(self, url: str) -> None:
        # Electron's shell.openExternal accepted any URL, including file:// and
        # custom protocol handlers. Only web/mail links are allowed now.
        scheme = urlparse(url or "").scheme.lower()
        if scheme not in _EXTERNAL_SCHEMES:
            raise HTTPException(400, f"refusing to open {scheme or 'empty'} URL")
        webbrowser.open(url)

    def open_cache_folder(self) -> dict[str, Any]:
        cache = paths.cache_dir()
        cache.mkdir(parents=True, exist_ok=True)
        try:
            open_with_system(str(cache))
            return {"ok": True, "path": str(cache), "error": None}
        except OSError as exc:
            return {"ok": False, "path": str(cache), "error": str(exc)}

    def fetch_exclusion_list(self, url: str) -> dict[str, Any]:
        if not isinstance(url, str) or not url:
            return {"text": None, "fromCache": False, "error": "No URL provided"}
        cache_path = paths.output_dir() / "exclusion-cache.txt"
        try:
            text = net.fetch_text(url, headers={"Cache-Control": "no-cache"}, timeout=20)
        except Exception as exc:  # noqa: BLE001
            try:
                cached = cache_path.read_text(encoding="utf-8")
                return {"text": cached, "fromCache": True, "error": f"Fetch failed, using cached version: {exc}"}
            except OSError:
                return {"text": None, "fromCache": False, "error": f"Fetch failed and no cache available: {exc}"}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=cache_path.parent, prefix=".exclusion-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, cache_path)
        except OSError:
            logger.warning("Could not write exclusion cache", exc_info=True)
        return {"text": text, "fromCache": False}

    def clear_scholar_session(self) -> dict[str, bool]:
        self.platform.clear_scholar_session()
        return {"ok": True}

    def install_update(self) -> None:
        if self.updater.launch_installer(relaunch=True):
            self.updater.install_on_quit = False
            self.platform.request_quit()


def build_router(services: HostServices) -> APIRouter:
    router = APIRouter(prefix="/__host", include_in_schema=False)

    @router.post("/rpc/{method}")
    def rpc(method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        args = (body or {}).get("args") or []
        if not isinstance(args, list):
            raise HTTPException(400, "args must be a list")
        try:
            return {"result": services.dispatch(method, args)}
        except HTTPException:
            raise
        except TypeError as exc:
            raise HTTPException(400, f"bad arguments for {method}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("host.%s failed", method)
            raise HTTPException(500, str(exc)) from exc

    @router.get("/pdf")
    def read_pdf(path: str = Query(...)) -> FileResponse:
        target = _require_pdf_path(path)
        if not target.is_file():
            raise HTTPException(404, "file not found")
        return FileResponse(target, media_type="application/pdf", headers={"Cache-Control": "no-store"})

    @router.put("/pdf")
    async def write_pdf(request: Request, path: str = Query(...)) -> dict[str, bool]:
        target = _require_pdf_path(path)
        if not target.parent.is_dir():
            raise HTTPException(400, "target directory does not exist")
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}-", suffix=".pdf.tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                async for chunk in request.stream():
                    fh.write(chunk)
            os.replace(tmp, target)
        except Exception as exc:
            Path(tmp).unlink(missing_ok=True)
            if isinstance(exc, PermissionError):
                raise HTTPException(403, f"cannot write {target}: {exc}") from exc
            raise
        return {"ok": True}

    return router
