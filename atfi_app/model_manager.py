"""One-time download of the citation NER model.

The Electron releases bundled the 125 MB ONNX model in every installer.
It now lives outside the install directory, in
``%LOCALAPPDATA%\\atfi-memnu-app\\models\\<name>\\<version>\\``. It is
downloaded once (normally by the installer, see packaging/installer.iss)
and reused by every later update.

This module is the app-side safety net. It handles portable builds, a
failed or skipped installer download, and a manifest version bump. In the
background it:

1. accepts a verified model already in place,
2. adopts a copy left by the Electron app's install (hash-checked),
3. otherwise downloads each file from the manifest mirrors with resume
   and SHA-256 verification,

then asks the backend to hot-load it. Until then NER extraction uses the
regex fallback, exactly as before when the model failed to load.

Progress is published as ``model:status`` host events (header pill in the UI).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import net
from .events import HostEvents

logger = logging.getLogger("atfi.model")

MANIFEST_PATH = Path(__file__).with_name("model_manifest.json")
MARKER = ".verified"


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_digest(manifest: dict[str, Any]) -> str:
    files = sorted((f["name"], f["sha256"]) for f in manifest["files"])
    return hashlib.sha256(json.dumps([manifest["version"], files]).encode()).hexdigest()


def legacy_electron_model_dirs() -> list[Path]:
    """Where Electron installs kept the bundled model (per-user NSIS layout)."""
    dirs: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for product in ("Kaynakça Kontrol Sistemi", "kaynakca-kontrol-sistemi", "atfi-memnu-app"):
            base = Path(local) / "Programs" / product / "resources" / "backend"
            dirs += [base / "_internal" / "models" / "citation-ner-int8", base / "models" / "citation-ner-int8"]
    return dirs


class ModelManager:
    def __init__(
        self,
        events: HostEvents,
        models_root: Path,
        *,
        manifest: dict[str, Any] | None = None,
        dev_model_dir: Path | None = None,
        legacy_dirs: list[Path] | None = None,
    ) -> None:
        self._events = events
        self._manifest = manifest or load_manifest()
        self._root = models_root / self._manifest["name"]
        self.model_dir = self._root / self._manifest["version"]
        self._dev_dir = dev_model_dir
        self._legacy_dirs = legacy_dirs if legacy_dirs is not None else legacy_electron_model_dirs()
        self._status: dict[str, Any] = {"state": "missing"}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._on_ready: Callable[[Path], None] | None = None

    # ---- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _set_status(self, status: dict[str, Any]) -> None:
        with self._lock:
            self._status = status
        self._events.publish("model:status", status)

    def ready_path(self) -> Path | None:
        """Directory of a usable model, if one is present right now."""
        if self._dev_dir is not None and self._has_files(self._dev_dir, check_hash=False):
            return self._dev_dir
        if self._is_verified(self.model_dir):
            return self.model_dir
        return None

    def _has_files(self, directory: Path, *, check_hash: bool) -> bool:
        for f in self._manifest["files"]:
            p = directory / f["name"]
            if not p.is_file() or p.stat().st_size != f["size"]:
                return False
            if check_hash and net.sha256_file(p) != f["sha256"]:
                return False
        return True

    def _is_verified(self, directory: Path) -> bool:
        marker = directory / MARKER
        try:
            if marker.read_text(encoding="utf-8").strip() != manifest_digest(self._manifest):
                return False
        except OSError:
            return False
        return self._has_files(directory, check_hash=False)

    # ---- background ensure -------------------------------------------------

    def start(self, on_ready: Callable[[Path], None]) -> None:
        """Make sure the model is present; call ``on_ready(dir)`` if it had to be fetched."""
        self._on_ready = on_ready
        if self.ready_path() is not None:
            self._set_status({"state": "ready"})
            self._cleanup_old_versions()
            return
        self._spawn()

    def retry(self) -> None:
        if self.status().get("state") in ("error", "missing"):
            self._spawn()

    def _spawn(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._ensure, name="model-download", daemon=True)
            self._thread.start()

    def _ensure(self) -> None:
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            if not self._adopt_legacy_copy():
                self._download_all()
            self._set_status({"state": "verifying"})
            if not self._has_files(self.model_dir, check_hash=True):
                raise OSError("Model files failed verification")
            (self.model_dir / MARKER).write_text(manifest_digest(self._manifest), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Model download failed")
            self._set_status({"state": "error", "message": str(exc)})
            return
        self._set_status({"state": "ready"})
        self._cleanup_old_versions()
        if self._on_ready is not None:
            try:
                self._on_ready(self.model_dir)
            except Exception:  # noqa: BLE001
                logger.exception("Model hot-reload failed")

    def _adopt_legacy_copy(self) -> bool:
        for src in self._legacy_dirs:
            if not self._has_files(src, check_hash=False):
                continue
            logger.info("Adopting NER model from previous install: %s", src)
            self._set_status({"state": "verifying"})
            for f in self._manifest["files"]:
                shutil.copy2(src / f["name"], self.model_dir / f["name"])
            if self._has_files(self.model_dir, check_hash=True):
                return True
            logger.warning("Legacy model copy failed verification; downloading instead")
        return False

    def _download_all(self) -> None:
        files = self._manifest["files"]
        total = sum(f["size"] for f in files)
        done_before: dict[str, int] = {}
        last_percent = -1.0

        def make_progress(name: str):
            def progress(received: int, _total: int) -> None:
                nonlocal last_percent
                done_before[name] = received
                got = sum(done_before.values())
                percent = got * 100.0 / total if total else 0.0
                if percent - last_percent >= 1.0 or percent >= 100:
                    last_percent = percent
                    self._set_status(
                        {"state": "downloading", "percent": percent, "receivedBytes": got, "totalBytes": total}
                    )

            return progress

        self._set_status({"state": "downloading", "percent": 0.0, "receivedBytes": 0, "totalBytes": total})
        for f in files:
            dest = self.model_dir / f["name"]
            if dest.is_file() and dest.stat().st_size == f["size"] and net.sha256_file(dest) == f["sha256"]:
                done_before[f["name"]] = f["size"]
                continue
            errors: list[str] = []
            for url in f["urls"]:
                try:
                    net.download_file(
                        url,
                        dest,
                        expected_sha256=f["sha256"],
                        expected_size=f["size"],
                        on_progress=make_progress(f["name"]),
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Download of %s from %s failed: %s", f["name"], url, exc)
                    errors.append(f"{url}: {exc}")
                    # A partial file from a different mirror must not be resumed.
                    dest.with_name(dest.name + ".part").unlink(missing_ok=True)
            else:
                raise OSError(f"Could not download {f['name']} ({'; '.join(errors)})")

    def _cleanup_old_versions(self) -> None:
        if not self._root.is_dir():
            return
        for child in self._root.iterdir():
            if child.is_dir() and child != self.model_dir:
                shutil.rmtree(child, ignore_errors=True)
