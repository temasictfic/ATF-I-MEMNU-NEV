import asyncio
import json
import logging
import re
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
logger = logging.getLogger("atfi.backend")

_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "success": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


# Some verifier errors embed the full request URL, which can carry API keys
# as query parameters (OpenAlex api_key, BASE apikey, PubMed api_key,
# OpenAIRE refreshToken). Never let those reach the log file.
_SECRET_QUERY_RE = re.compile(
    r"((?:api_?key|apikey|refresh_?token|access_?token|token|key)=)[^&\s\"']+",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    return _SECRET_QUERY_RE.sub(r"\1***", text)


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self._send_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)

    async def broadcast(self, event_type: str, data: dict[str, Any]):
        message = json.dumps({"type": event_type, "data": data})
        disconnected = []
        async with self._send_lock:
            # Iterate over a snapshot: disconnect() can mutate the list while
            # we await a send, which made the loop skip a client.
            for ws in list(self.connections):
                try:
                    await ws.send_text(message)
                except Exception:  # noqa: BLE001
                    disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)

    async def send_log(self, level: str, message: str, **kwargs):
        """Record a backend progress/log line.

        This used to broadcast a ``log_entry`` WebSocket event to the
        renderer, which never listened for it: about 35 call sites
        serialized and sent every line for nothing. Now it goes to the
        application log file (see ``atfi_app.logging_setup``).
        """
        extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
        line = redact_secrets(f"{message} [{extra}]" if extra else message)
        logger.log(_LOG_LEVELS.get(level, logging.INFO), "%s", line)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # The renderer never sends anything; reading just keeps the
            # connection open until the client goes away.
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:  # noqa: BLE001
        logger.warning("WS endpoint error: %s: %s", type(e).__name__, e)
        manager.disconnect(websocket)
