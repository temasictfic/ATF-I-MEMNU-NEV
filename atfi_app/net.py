"""Host-side HTTPS helpers (updater, model download, exclusion list).

Electron's ``net.fetch`` used Chromium's network stack, which trusts the
Windows certificate store, so it worked behind TLS-inspecting corporate
proxies. ``truststore`` gives Python the same behaviour. ``certifi`` is
kept as a fallback where truststore is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import ssl
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError

from .version import __version__

logger = logging.getLogger("atfi.net")

USER_AGENT = f"AtfiMemnu/{__version__} (+https://github.com/temasictfic/ATF-I-MEMNU-NEV)"

_ctx_lock = threading.Lock()
_ctx: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    global _ctx
    with _ctx_lock:
        if _ctx is None:
            try:
                import truststore

                _ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            except Exception:  # noqa: BLE001
                import certifi

                _ctx = ssl.create_default_context(cafile=certifi.where())
        return _ctx


def open_url(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(request, timeout=timeout, context=ssl_context())  # noqa: S310


def fetch_text(url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0) -> str:
    with open_url(url, headers=headers, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


class DownloadCancelled(Exception):
    pass


def download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = 60.0,
) -> Path:
    """Download ``url`` to ``dest`` via ``dest.part`` with resume and hash check.

    A partial file is resumed with an HTTP Range request when the server
    supports it. ``on_progress(received, total)`` is called as bytes arrive.
    The final file only appears at ``dest`` once fully downloaded and (when
    given) verified.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    offset = part.stat().st_size if part.exists() else 0
    if expected_size is not None and offset > expected_size:
        part.unlink()
        offset = 0

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    try:
        resp = open_url(url, headers=headers, timeout=timeout)
    except HTTPError as e:
        if e.code == 416 and offset:  # range not satisfiable: restart cleanly
            part.unlink(missing_ok=True)
            return download_file(
                url, dest, expected_sha256=expected_sha256, expected_size=expected_size,
                on_progress=on_progress, cancel=cancel, timeout=timeout,
            )
        raise

    with resp:
        if offset and resp.status != 206:
            offset = 0  # server ignored Range: start over
        total_header = resp.headers.get("Content-Length")
        total = expected_size or ((int(total_header) + offset) if total_header else 0)
        mode = "ab" if offset else "wb"
        received = offset
        with open(part, mode) as fh:
            while True:
                if cancel is not None and cancel.is_set():
                    raise DownloadCancelled()
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
                if on_progress:
                    on_progress(received, total)

    if expected_size is not None and part.stat().st_size != expected_size:
        raise OSError(f"Incomplete download of {dest.name}: {part.stat().st_size} of {expected_size} bytes")
    if expected_sha256:
        digest = sha256_file(part)
        if digest.lower() != expected_sha256.lower():
            part.unlink(missing_ok=True)
            raise OSError(f"Checksum mismatch for {dest.name}")
    part.replace(dest)
    return dest


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_transient_network_error(exc: BaseException) -> bool:
    """Equivalent of Electron's IGNORED_UPDATE_ERRORS for background checks."""
    if isinstance(exc, HTTPError):
        return exc.code in (403, 404, 429) or exc.code >= 500
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, ssl.SSLError, OSError))
