"""Persistence for the Parsing page's "last session" PDF list.

Electron kept this in renderer localStorage. The UI origin now includes a
per-launch port, and localStorage is per-origin, so the host stores it in
``%APPDATA%\\atfi-memnu-app\\session-pdfs.json`` and injects the previous
run's list at boot.

This also fixes an old bug. The previous run's list was cleared at startup
even if the new run imported nothing, so launching the app twice lost the
list. Now ``rotate()`` only replaces ``last`` when the previous run
actually imported something.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("atfi.session")

_MAX_ENTRIES = 500


def _clean(entries: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(entries, list):
        return out
    for e in entries[:_MAX_ENTRIES]:
        if not isinstance(e, dict):
            continue
        path, name, imported_at = e.get("path"), e.get("name"), e.get("importedAt")
        if not isinstance(path, str) or not path:
            continue
        out.append(
            {
                "path": path,
                "name": name if isinstance(name, str) else os.path.basename(path),
                "importedAt": imported_at if isinstance(imported_at, (int, float)) else 0,
            }
        )
    return out


class SessionStore:
    def __init__(self, file: Path) -> None:
        self._file = file
        self._lock = threading.Lock()
        self._last: list[dict[str, Any]] = []

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._file.parent, prefix=".session-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False)
            os.replace(tmp, self._file)
        except OSError:
            logger.exception("could not save session list")
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def rotate(self) -> list[dict[str, Any]]:
        """Start a new run: promote the previous run's imports to "last"."""
        with self._lock:
            data = self._read()
            current = _clean(data.get("current"))
            last = current if current else _clean(data.get("last"))
            self._last = last
            self._write({"last": last, "current": []})
            return list(last)

    @property
    def last(self) -> list[dict[str, Any]]:
        return list(self._last)

    def save_current(self, entries: Any) -> None:
        with self._lock:
            self._write({"last": self._last, "current": _clean(entries)})
