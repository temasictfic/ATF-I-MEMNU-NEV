"""Host → page events.

Electron pushed updater events to the renderer with
``webContents.send``. The host pushes events on the backend WebSocket the
page already keeps open: ``{"type": "host_event", "data": {"channel",
"payload"}}``. The frontend's ``onHostEvent`` (lib/host/bridge.ts)
dispatches them.

``publish`` is thread-safe and never blocks. It can be called from the
WinForms UI thread, worker threads or the server loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

logger = logging.getLogger("atfi.events")


class HostEvents:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()
        self._backlog: list[tuple[str, Any]] = []

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called from the backend lifespan with the server's event loop."""
        with self._lock:
            self._loop = loop
            backlog, self._backlog = self._backlog, []
        for channel, payload in backlog:
            self.publish(channel, payload)

    def publish(self, channel: str, payload: Any = None) -> None:
        with self._lock:
            loop = self._loop
            if loop is None:
                # Before the server is up: keep it (bounded) and replay later.
                if len(self._backlog) < 200:
                    self._backlog.append((channel, payload))
                return
        if loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(channel, payload), loop)
        except RuntimeError:
            pass  # loop shutting down

    @staticmethod
    async def _broadcast(channel: str, payload: Any) -> None:
        from api.websocket import manager  # backend package (on sys.path)

        try:
            await manager.broadcast("host_event", {"channel": channel, "payload": payload})
        except Exception:  # noqa: BLE001
            logger.exception("host event %s failed", channel)
