"""Runs the FastAPI backend in-process (replaces src/main/python-bridge.ts).

Electron spawned a separate PyInstaller executable, picked a "free" port by
opening and closing a probe socket (a TOCTOU race the old code
acknowledged), polled /api/health and /api/ready over HTTP, and killed the
process tree with ``taskkill /F`` on exit, which could truncate cache
files.

Here the host binds the loopback socket itself (``SO_EXCLUSIVEADDRUSE`` on
Windows; uvicorn's own bind sets ``SO_REUSEADDR``, which permits port
hijacking there) and hands it to uvicorn, which serves on a background
thread. Readiness is read in-process, and shutdown is graceful: the
lifespan cancels verification work and flushes state before the loop exits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger("atfi.backend_host")


def bind_loopback_socket(port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def prepare_backend_import(backend_dir: Path, *, output_dir: Path, model_dir: Path | None) -> None:
    """Configure environment + sys.path before anything imports the backend.

    The backend reads ``ATFI_*`` settings at import time (pydantic-settings),
    so this must run first.
    """
    os.environ["ATFI_OUTPUT_DIR"] = str(output_dir)
    os.environ["PYTHONUTF8"] = "1"
    if model_dir is not None:
        os.environ["ATFI_NER_LOCAL_MODEL_PATH"] = str(model_dir)
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))


class BackendHost:
    def __init__(
        self,
        *,
        frontend_dir: Path | None,
        boot_info: Callable[[], dict[str, Any]],
        extra_routers: list[Any],
        on_loop_ready: Callable[[asyncio.AbstractEventLoop], None] | None = None,
        port: int = 0,
        dev_origins: list[str] | None = None,
        token: str | None = None,
    ) -> None:
        self.token = token if token is not None else secrets.token_urlsafe(32)
        self._sock = bind_loopback_socket(port)
        self.port: int = self._sock.getsockname()[1]
        self._frontend_dir = frontend_dir
        self._boot_info = boot_info
        self._extra_routers = extra_routers
        self._dev_origins = dev_origins
        self._user_loop_cb = on_loop_ready
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server = None
        self._thread: threading.Thread | None = None
        self._failure: BaseException | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def _on_loop_ready(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        if self._user_loop_cb is not None:
            self._user_loop_cb(loop)

    def start(self) -> None:
        import uvicorn
        from config import settings  # backend package
        from main import create_app

        settings.port = self.port
        app = create_app(
            token=self.token,
            frontend_dir=self._frontend_dir,
            boot_info=self._boot_info,
            dev_origins=self._dev_origins,
            on_loop_ready=self._on_loop_ready,
            extra_routers=self._extra_routers,
        )
        config = uvicorn.Config(
            app,
            log_config=None,  # keep the host's logging configuration
            ws="websockets-sansio",
            lifespan="on",
            timeout_graceful_shutdown=5,
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        def run() -> None:
            try:
                self._server.run(sockets=[self._sock])
            except BaseException as exc:  # noqa: BLE001 - includes uvicorn's SystemExit
                self._failure = exc
                logger.exception("Backend server stopped with an error")

        self._thread = threading.Thread(target=run, name="backend", daemon=True)
        self._thread.start()

    def wait_started(self, timeout: float = 60.0) -> None:
        """Block until uvicorn is serving. Raises with the failure reason otherwise."""
        assert self._thread is not None
        deadline = timeout
        step = 0.05
        while deadline > 0:
            if self._server is not None and self._server.started:
                return
            if not self._thread.is_alive():
                raise RuntimeError(f"Backend exited during startup: {self._failure!r}")
            self._thread.join(step)
            deadline -= step
        raise TimeoutError(f"Backend did not start within {timeout:.0f}s")

    def run_coroutine(self, coro) -> Any:  # noqa: ANN001
        """Schedule a coroutine on the server loop (thread-safe); returns a Future."""
        if self._loop is None:
            raise RuntimeError("Backend loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def reload_ner(self, model_dir: Path) -> None:
        from services.ner_model_manager import reload_pipeline

        ok = self.run_coroutine(reload_pipeline(str(model_dir))).result(timeout=300)
        logger.info("NER model hot-reload %s", "succeeded" if ok else "failed")

    def stop(self, timeout: float = 15.0) -> None:
        if self._server is None or self._thread is None:
            return
        self._server.should_exit = True
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("Backend did not stop within %.0fs; forcing exit", timeout)
            self._server.force_exit = True
            self._thread.join(3)
