"""Application logging.

Electron only logged to a console that packaged builds never showed. The
host now writes a rotating log file to ``%LOCALAPPDATA%\\atfi-memnu-app\\logs``
and also logs to stderr in development. In a windowed (no-console)
PyInstaller build ``sys.stdout``/``sys.stderr`` are ``None``: they are
pointed at the log so stray prints and tracebacks aren't lost, and so
libraries that call ``sys.stdout.isatty()`` don't crash.
"""

from __future__ import annotations

import io
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _LogWriter(io.TextIOBase):
    def __init__(self, logger: logging.Logger, level: int) -> None:
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, text: str) -> int:  # type: ignore[override]
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.rstrip())
        self._buffer = ""

    def isatty(self) -> bool:
        return False


def configure(log_dir: Path, *, verbose: bool = False) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "app.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)
    else:
        sys.stderr = _LogWriter(logging.getLogger("stderr"), logging.ERROR)  # type: ignore[assignment]
    if sys.stdout is None:
        sys.stdout = _LogWriter(logging.getLogger("stdout"), logging.INFO)  # type: ignore[assignment]

    # Quiet chatty libraries.
    for name in ("uvicorn.access", "pywebview", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    return log_file
