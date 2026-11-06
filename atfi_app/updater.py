"""In-app updater (replaces electron-updater).

The behaviour mirrors the Electron build (src/main/index.ts):

* One background check runs about 5 s after start-up. A newer release
  publishes ``update:available``; the latest version publishes
  ``update:notAvailable``.
* The download starts only when the user clicks Download. Progress goes out
  as ``update:progress`` (percent) and completion as ``update:downloaded``.
  Cancel stops the download and also prevents install-on-quit.
* Install runs the downloaded Inno Setup installer silently and quits.
  An update that was downloaded but not installed is applied on quit
  (``autoInstallOnAppQuit``).
* Background-check failures never reach the UI. Errors during a
  user-initiated download always do. electron-updater swallowed
  "ignorable" network errors even then, which left the UI stuck on its
  progress bar.

Releases come from GitHub (``RELEASE_REPO``). The installer asset is
verified against the release's ``SHA256SUMS`` file before it is run.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import net
from .events import HostEvents
from .version import CHECKSUMS_ASSET, RELEASE_REPO, SETUP_ASSET_TEMPLATE, __version__

logger = logging.getLogger("atfi.updater")

API_LATEST = "https://api.github.com/repos/{repo}/releases/latest"


def parse_version(text: str) -> tuple[int, ...] | None:
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return None
    return tuple(int(p) for p in m.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


@dataclass
class UpdateState:
    stage: str = "idle"  # idle | available | downloading | ready | error
    version: str | None = None
    notes: str | None = None
    percent: float = 0.0
    error: str | None = None
    installer: Path | None = None
    asset_url: str | None = None
    checksums_url: str | None = None
    asset_name: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Updater:
    def __init__(
        self,
        events: HostEvents,
        download_dir: Path,
        *,
        enabled: bool,
        repo: str = RELEASE_REPO,
        current_version: str = __version__,
        api_url_template: str = API_LATEST,
    ) -> None:
        self._events = events
        self._dir = download_dir
        self.enabled = enabled
        self._repo = repo
        self._current = current_version
        self._api_url = api_url_template.format(repo=repo)
        self._state = UpdateState()
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._download_thread: threading.Thread | None = None
        self._startup_check_done = False
        self.install_on_quit = True

    # ---- state -------------------------------------------------------------

    def state(self) -> dict[str, Any] | None:
        with self._lock:
            s = self._state
            if s.stage == "idle":
                return None
            return {"stage": s.stage, "version": s.version, "percent": s.percent, "error": s.error}

    # ---- check -------------------------------------------------------------

    def schedule_startup_check(self, delay: float = 5.0) -> None:
        if not self.enabled or self._startup_check_done:
            return
        self._startup_check_done = True
        timer = threading.Timer(delay, self._background_check)
        timer.daemon = True
        timer.start()

    def _background_check(self) -> None:
        try:
            self.check()
        except Exception as exc:  # noqa: BLE001
            # Same policy as Electron: a silent start-up check never surfaces
            # an error banner.
            logger.warning("Update check failed (suppressed): %s", exc)

    def check(self) -> bool:
        """Query the latest release. Returns True when an update is available."""
        raw = net.fetch_text(
            self._api_url, headers={"Accept": "application/vnd.github+json"}, timeout=20
        )
        release = json.loads(raw)
        tag = str(release.get("tag_name") or "")
        version = tag.lstrip("vV")
        if not is_newer(version, self._current):
            self._events.publish("update:notAvailable")
            return False

        asset_name = SETUP_ASSET_TEMPLATE.format(version=version)
        assets = {a.get("name"): a.get("browser_download_url") for a in release.get("assets") or []}
        if asset_name not in assets or CHECKSUMS_ASSET not in assets:
            logger.warning("Release %s lacks %s or %s; ignoring", tag, asset_name, CHECKSUMS_ASSET)
            self._events.publish("update:notAvailable")
            return False

        with self._lock:
            if self._state.stage in ("downloading", "ready") and self._state.version == version:
                return True
            self._state = UpdateState(
                stage="available",
                version=version,
                notes=release.get("body"),
                asset_url=assets[asset_name],
                checksums_url=assets[CHECKSUMS_ASSET],
                asset_name=asset_name,
            )
        self._events.publish("update:available", {"version": version, "releaseNotes": release.get("body")})
        return True

    # ---- download ----------------------------------------------------------

    def download(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._state.stage not in ("available", "error") or not self._state.asset_url:
                return
            if self._download_thread and self._download_thread.is_alive():
                return
            self._cancel.clear()
            self.install_on_quit = True
            self._state.stage = "downloading"
            self._state.percent = 0.0
            self._state.error = None
            self._download_thread = threading.Thread(target=self._download, name="update-download", daemon=True)
            self._download_thread.start()

    def _download(self) -> None:
        with self._lock:
            state = self._state
        assert state.asset_url and state.checksums_url and state.asset_name
        last_sent = -1.0

        def progress(received: int, total: int) -> None:
            nonlocal last_sent
            if not total:
                return
            percent = received * 100.0 / total
            with self._lock:
                self._state.percent = percent
            if percent - last_sent >= 0.5 or percent >= 100:
                last_sent = percent
                self._events.publish("update:progress", {"percent": percent})

        try:
            sums = net.fetch_text(state.checksums_url, timeout=30)
            expected = _checksum_for(sums, state.asset_name)
            if not expected:
                raise RuntimeError(f"{CHECKSUMS_ASSET} has no entry for {state.asset_name}")
            target = self._dir / state.asset_name
            if target.exists() and net.sha256_file(target).lower() == expected.lower():
                pass  # already downloaded earlier
            else:
                net.download_file(
                    state.asset_url, target, expected_sha256=expected, on_progress=progress, cancel=self._cancel
                )
        except net.DownloadCancelled:
            logger.info("Update download cancelled")
            with self._lock:
                self._state.stage = "available"
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Update download failed")
            with self._lock:
                self._state.stage = "error"
                self._state.error = str(exc)
            if not self._cancel.is_set():
                self._events.publish("update:error", str(exc))
            return

        with self._lock:
            self._state.stage = "ready"
            self._state.percent = 100.0
            self._state.installer = target
        self._events.publish("update:downloaded")

    def cancel(self) -> None:
        self._cancel.set()
        # Mirrors Electron: also prevent an already-downloaded update from
        # installing on quit.
        self.install_on_quit = False

    # ---- install -----------------------------------------------------------

    def installer_path(self) -> Path | None:
        with self._lock:
            return self._state.installer if self._state.stage == "ready" else None

    def launch_installer(self, *, relaunch: bool) -> bool:
        installer = self.installer_path()
        if installer is None or not installer.exists():
            return False
        args = [str(installer), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]
        if relaunch:
            args.append("/RELAUNCH")  # handled by packaging/installer.iss
        else:
            args[1] = "/VERYSILENT"
        logger.info("Launching installer: %s", args)
        creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(args, close_fds=True, creationflags=creationflags)  # noqa: S603
        return True

    def install_pending_on_quit(self) -> bool:
        """Start a downloaded update's installer; True if one was launched."""
        if self.enabled and self.install_on_quit and self.installer_path() is not None:
            return self.launch_installer(relaunch=False)
        return False


def _checksum_for(sums_text: str, name: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0]
    return None
