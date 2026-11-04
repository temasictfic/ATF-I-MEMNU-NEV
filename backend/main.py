"""FastAPI application factory.

The desktop host (``atfi_app.backend_host``) imports this module in-process
and runs the app with uvicorn on a background thread, on a loopback socket
it bound itself. The same app also serves the built React UI from that
origin, so the WebView2 page and the API share one origin: no CORS, no
mixed-content or local-network-access issues, and the per-launch token can
be injected straight into ``index.html``.

``python main.py --port 8765`` still runs the API standalone for backend
development. That mode has no token unless ``--token`` is given.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from config import settings
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from security import LoopbackGuard
from starlette.staticfiles import StaticFiles

logger = logging.getLogger("atfi.backend")

BootInfoProvider = Callable[[], dict[str, Any]]
LoopCallback = Callable[[asyncio.AbstractEventLoop], None]


def _make_lifespan(on_loop_ready: LoopCallback | None):
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from services.background_tasks import spawn
        from services.ner_model_manager import preload_pipeline

        logger.info("Atf-ı Memnu backend starting on port %s", settings.port)
        Path(settings.output_dir).mkdir(parents=True, exist_ok=True)
        if on_loop_ready is not None:
            on_loop_ready(asyncio.get_running_loop())

        # Preload NER in the background so the first extraction is instant.
        spawn(preload_pipeline(), name="ner-preload")

        yield

        # Shutdown order matters: stop verification work first so nothing is
        # still using the HTTP session (or writing caches) while we tear it
        # down, then close the session, the NER executor and the quota file.
        logger.info("Atf-ı Memnu backend shutting down")
        from services.background_tasks import cancel_all
        from services.daily_quota import daily_quota
        from services.ner_model_manager import shutdown_inference_executor
        from services.verification_orchestrator import cancel_all_active
        from verifiers._http import close_session

        cancel_all_active()
        await cancel_all(timeout=5.0)
        await close_session()
        shutdown_inference_executor()
        daily_quota.stop_background_flush()

    return lifespan


def _index_response(frontend_dir: Path, boot_info: BootInfoProvider | None) -> Response:
    index = (frontend_dir / "index.html").read_text(encoding="utf-8")
    if boot_info is not None:
        # `</` is escaped so no value can terminate the script element.
        payload = json.dumps(boot_info(), ensure_ascii=False).replace("</", "<\\/")
        tag = f"<script>window.__ATFI__ = {payload};</script>"
        index = index.replace("<head>", "<head>\n    " + tag, 1)
    return HTMLResponse(
        index,
        headers={
            "Cache-Control": "no-store",
            # The page carries the API token: never allow it to be framed.
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "frame-ancestors 'none'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


def create_app(
    *,
    token: str | None = None,
    frontend_dir: str | Path | None = None,
    boot_info: BootInfoProvider | None = None,
    dev_origins: list[str] | None = None,
    on_loop_ready: LoopCallback | None = None,
) -> FastAPI:
    """Build the API (plus, optionally, the static UI) app.

    token:         per-launch secret required on /api/* (None disables it).
    frontend_dir:  built UI (``frontend/dist``) to serve at ``/``.
    boot_info:     returns the ``window.__ATFI__`` object injected into index.html.
    dev_origins:   extra origins (e.g. the Vite dev server) allowed via CORS.
    on_loop_ready: called with the server's event loop during startup, so the
                   host can schedule coroutines on it thread-safely.
    """
    app = FastAPI(
        title="Atf-ı Memnu API",
        version="1.0.0",
        lifespan=_make_lifespan(on_loop_ready),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    from api.router import api_router

    app.include_router(api_router, prefix="/api")

    if dev_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=dev_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["Content-Type", "X-Atfi-Token"],
        )

    if frontend_dir is not None:
        dist = Path(frontend_dir)
        if not (dist / "index.html").is_file():
            raise RuntimeError(f"Frontend build not found at {dist} (run `npm run build` in frontend/)")

        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        async def index() -> Response:
            return _index_response(dist, boot_info)

        app.mount("/", StaticFiles(directory=dist), name="ui")

    app.add_middleware(LoopbackGuard, token=token, extra_origins=dev_origins or ())
    return app


def _run_standalone() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Atf-ı Memnu API standalone (development).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", type=str, default=None)
    parser.add_argument("--frontend", type=str, default=None, help="Path to frontend/dist to serve at /")
    args = parser.parse_args()

    settings.port = args.port
    logging.basicConfig(level=logging.INFO)
    boot = (lambda: {"port": args.port, "token": args.token}) if args.frontend else None
    app = create_app(
        token=args.token,
        frontend_dir=args.frontend,
        boot_info=boot,
        dev_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, ws="websockets-sansio", log_level="info")


if __name__ == "__main__":
    _run_standalone()
