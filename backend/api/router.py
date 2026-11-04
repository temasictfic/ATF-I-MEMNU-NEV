from config import settings as app_config
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from services.ner_model_manager import (
    get_load_error,
    is_loading,
    is_model_ready,
    is_preload_complete,
)

from api.parsing import router as parsing_router
from api.settings import router as settings_router
from api.verification import router as verification_router
from api.websocket import router as ws_router

api_router = APIRouter()


@api_router.get("/health")
async def health():
    """Liveness probe: returns 200 as soon as uvicorn is serving.

    Use /api/ready to wait until the backend is fully initialized
    (NER preload resolved, either loaded or committed to regex).
    """
    return {"status": "ok", "version": "1.0.0"}


@api_router.get("/ready")
async def ready():
    """Readiness probe: 200 once preload_pipeline() has finished trying.

    Covers the first-launch-after-auto-update window where the 125 MB
    ONNX model is still warming up. The main process polls this endpoint
    before unblocking the UI so the user never sees the verify button
    while NER extraction would silently fall through to regex on garbage
    queries.

    Returns 200 with a descriptive `ner` state field when preload is
    done, 503 while it is still loading. The caller can surface the
    state to the user.
    """
    if not app_config.ner_enabled:
        return {"status": "ready", "ner": "disabled"}
    if is_preload_complete():
        if is_model_ready():
            return {"status": "ready", "ner": "loaded"}
        return {
            "status": "ready",
            "ner": "failed",
            "error": get_load_error() or "unknown",
        }
    return JSONResponse(
        status_code=503,
        content={"status": "initializing", "ner": "loading" if is_loading() else "pending"},
    )


@api_router.post("/shutdown")
async def do_shutdown():
    """Graceful shutdown endpoint called by Electron before process termination."""
    import server_state
    if server_state.uvicorn_server:
        server_state.uvicorn_server.should_exit = True
    return {"status": "shutting_down"}


api_router.include_router(parsing_router)
api_router.include_router(verification_router)
api_router.include_router(settings_router)
api_router.include_router(ws_router)
