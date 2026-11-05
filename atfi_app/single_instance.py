"""Single running instance (replaces Electron's requestSingleInstanceLock).

The first instance listens on a per-user local endpoint: a named pipe on
Windows, a Unix socket elsewhere. A second launch connects, asks the first
instance to bring its window to the front, and exits. Without this a
double-click would start a second backend and a second updater.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from multiprocessing.connection import Client, Listener
from pathlib import Path

logger = logging.getLogger("atfi.single_instance")

_MESSAGE = "activate"


def _endpoint(runtime_dir: Path) -> tuple[str, str]:
    user = hashlib.sha256(getpass.getuser().encode("utf-8", "replace")).hexdigest()[:16]
    if sys.platform == "win32":
        return rf"\\.\pipe\atfi-memnu-app-{user}", "AF_PIPE"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return str(runtime_dir / f"instance-{user}.sock"), "AF_UNIX"


def _authkey() -> bytes:
    return hashlib.sha256(f"atfi-memnu:{getpass.getuser()}".encode()).digest()


class SingleInstance:
    def __init__(self, runtime_dir: Path) -> None:
        self._address, self._family = _endpoint(runtime_dir)
        self._listener: Listener | None = None

    def notify_existing(self) -> bool:
        """True if another instance is running (and was asked to activate)."""
        try:
            conn = Client(self._address, family=self._family, authkey=_authkey())
        except (OSError, EOFError):
            return False
        try:
            conn.send(_MESSAGE)
        finally:
            conn.close()
        return True

    def listen(self, on_activate: Callable[[], None]) -> None:
        if self._family == "AF_UNIX" and os.path.exists(self._address):
            os.unlink(self._address)  # stale socket from a crashed run
        self._listener = Listener(self._address, family=self._family, authkey=_authkey())

        def serve() -> None:
            while True:
                try:
                    conn = self._listener.accept()  # type: ignore[union-attr]
                except (OSError, EOFError):
                    return
                except Exception:  # noqa: BLE001  (bad authkey etc.)
                    continue
                try:
                    if conn.recv() == _MESSAGE:
                        on_activate()
                except Exception:  # noqa: BLE001
                    logger.debug("ignoring malformed activation request", exc_info=True)
                finally:
                    conn.close()

        threading.Thread(target=serve, name="single-instance", daemon=True).start()

    def close(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
