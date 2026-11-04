"""Crash-safe file writes.

Every persisted JSON file (settings, sources cache, verification cache) used
to be written with a plain ``Path.write_text``. If the process died mid-write
the file was left truncated, and for ``settings.json`` that was destructive:
a corrupt file loads as ``{}``, the database migration then saves defaults
over it, and every stored API key is lost.

``atomic_write_text`` writes to a temporary file in the same directory,
fsyncs it, and swaps it into place with ``os.replace`` (atomic on NTFS and
POSIX), so readers only ever see the old file or the new one.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))
