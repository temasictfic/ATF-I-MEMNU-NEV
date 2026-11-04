import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class ConnectionManager:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self._send_lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.connections.append(websocket)
        # print(f"[WS] Client connected. Total: {len(self.connections)}", flush=True)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.connections:
            self.connections.remove(websocket)
        # print(f"[WS] Client disconnected. Total: {len(self.connections)}", flush=True)

    async def broadcast(self, event_type: str, data: dict[str, Any]):
        message = json.dumps({"type": event_type, "data": data})
        # if event_type.startswith("verify_"):
        #     print(f"[WS Broadcast] {event_type} to {len(self.connections)} client(s)", flush=True)
        disconnected = []
        async with self._send_lock:
            for ws in self.connections:
                try:
                    await ws.send_text(message)
                except Exception:  # noqa: BLE001
                    # print(f"[WS Broadcast] Send failed: {type(e).__name__}: {e}", flush=True)
                    disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)

    async def send_log(self, level: str, message: str, **kwargs):
        from datetime import datetime, timezone
        data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": level,
            "message": message,
            **kwargs,
        }
        await self.broadcast("log_entry", data)


manager = ConnectionManager()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                json.loads(data)
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:  # noqa: BLE001
        print(f"[WS] Endpoint error: {type(e).__name__}: {e}", flush=True)
        manager.disconnect(websocket)
