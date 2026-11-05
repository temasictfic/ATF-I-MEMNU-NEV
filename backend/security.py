"""Loopback hardening for the backend API.

The Electron build exposed the API with ``allow_origins=["*"]`` plus
``allow_credentials=True`` and no authentication at all. Any web page open
in the user's browser could scan localhost for the port, read
``GET /api/settings`` (which returns every stored API key and the OpenAIRE
refresh token) and drive verification. Nothing checked the Host header
either, so DNS rebinding worked too.

The desktop host now serves the UI from this same origin
(``http://127.0.0.1:<port>/``), so the API needs no CORS at all, and this
ASGI middleware adds three checks:

* **Host allow-list**: only ``127.0.0.1``/``localhost`` Host headers are
  accepted. This blocks DNS rebinding.
* **Per-launch token** on every ``/api/*`` and ``/__host/*`` request
  (the desktop host's file/dialog bridge): the ``X-Atfi-Token`` header for
  HTTP, and the ``token`` query parameter for the WebSocket, where
  browsers cannot set headers.
* **WebSocket Origin check**: CORS does not apply to WebSockets, so the
  Origin header is validated explicitly.

``/api/health`` stays unauthenticated. It returns nothing sensitive and is
handy for diagnostics.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from urllib.parse import parse_qs

ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost"})
TOKEN_HEADER = b"x-atfi-token"
_PUBLIC_PATHS = frozenset({"/api/health"})
PROTECTED_PREFIXES = ("/api/", "/__host/")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key == name:
            return value.decode("latin-1")
    return None


def _hostname(host_header: str | None) -> str | None:
    if not host_header:
        return None
    host = host_header.strip().lower()
    if host.startswith("["):  # IPv6 literal, e.g. [::1]:8000
        return host[1 : host.find("]")] if "]" in host else None
    return host.rsplit(":", 1)[0] if ":" in host else host


class LoopbackGuard:
    def __init__(self, app, *, token: str | None, extra_origins: Iterable[str] = ()):
        self.app = app
        self.token = token
        self.extra_origins = {o.rstrip("/") for o in extra_origins}

    def _origin_allowed(self, scope, origin: str | None) -> bool:
        if origin is None:
            return True  # non-browser client (it still needs the token)
        origin = origin.rstrip("/")
        if origin in self.extra_origins:
            return True
        host = _header(scope, b"host") or ""
        return origin in {f"http://{host}", f"https://{host}"}

    def _token_ok(self, presented: str | None) -> bool:
        if self.token is None:
            return True
        return presented is not None and hmac.compare_digest(presented, self.token)

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        if _hostname(_header(scope, b"host")) not in ALLOWED_HOSTNAMES:
            return await self._reject(kind, send, 400, b"Invalid host")

        path: str = scope.get("path", "")
        if path.startswith(PROTECTED_PREFIXES) and path not in _PUBLIC_PATHS:
            if kind == "websocket":
                query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
                presented = (query.get("token") or [None])[0]
                if not self._origin_allowed(scope, _header(scope, b"origin")):
                    return await self._reject(kind, send, 403, b"Origin not allowed")
            else:
                presented = _header(scope, TOKEN_HEADER)
                # CORS preflights from the dev server carry no custom headers.
                if scope.get("method") == "OPTIONS" and self.extra_origins:
                    return await self.app(scope, receive, send)
            if not self._token_ok(presented):
                return await self._reject(kind, send, 401, b"Missing or invalid token")

        return await self.app(scope, receive, send)

    @staticmethod
    async def _reject(kind: str, send, status: int, body: bytes) -> None:
        if kind == "websocket":
            # Closing before accept makes the server answer the handshake with 403.
            await send({"type": "websocket.close", "code": 1008})
            return
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"text/plain; charset=utf-8")],
            }
        )
        await send({"type": "http.response.body", "body": body})
