"""Filesystem locations used by the desktop host.

Roaming user data (settings, API keys, caches) lives where the Electron build
kept it, ``%APPDATA%\\atfi-memnu-app``, so an upgrade from the Electron app
keeps everything. Machine-local, re-creatable data (WebView2 profile, the
downloaded NER model, update installers, logs) goes under
``%LOCALAPPDATA%\\atfi-memnu-app`` so it doesn't roam.

On non-Windows systems (development and CI only) XDG-style directories are
used. Every root can be overridden by environment variables, which the
tests and dev tooling use:

* ``ATFI_USER_DATA_DIR``
* ``ATFI_LOCAL_DATA_DIR``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# MUST NOT change: the historical Electron userData folder name
# (package.json#name), kept so settings survive the port.
USER_DATA_DIR_NAME = "atfi-memnu-app"

IS_WINDOWS = sys.platform == "win32"
IS_FROZEN = bool(getattr(sys, "frozen", False))


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def user_data_dir() -> Path:
    override = _env_path("ATFI_USER_DATA_DIR")
    if override:
        return override
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / USER_DATA_DIR_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / USER_DATA_DIR_NAME


def local_data_dir() -> Path:
    override = _env_path("ATFI_LOCAL_DATA_DIR")
    if override:
        return override
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / USER_DATA_DIR_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / USER_DATA_DIR_NAME


def output_dir() -> Path:
    """Backend data: settings.json, cache/, quota_state.json, exclusion cache."""
    return user_data_dir() / "output"


def cache_dir() -> Path:
    return output_dir() / "cache"


def session_file() -> Path:
    return user_data_dir() / "session-pdfs.json"


def webview2_data_dir() -> Path:
    return local_data_dir() / "WebView2"


def models_dir() -> Path:
    return local_data_dir() / "models"


def updates_dir() -> Path:
    return local_data_dir() / "updates"


def logs_dir() -> Path:
    return local_data_dir() / "logs"


def bundle_root() -> Path:
    """Directory holding bundled resources (PyInstaller _MEIPASS or the repo root)."""
    if IS_FROZEN:
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def backend_dir() -> Path:
    return bundle_root() / "backend"


def frontend_dist_dir() -> Path:
    override = _env_path("ATFI_FRONTEND_DIST")
    if override:
        return override
    if IS_FROZEN:
        return bundle_root() / "frontend_dist"
    return bundle_root() / "frontend" / "dist"


def icon_path() -> Path:
    return bundle_root() / "resources" / "icon.ico"


def install_dir() -> Path:
    return Path(sys.executable).resolve().parent if IS_FROZEN else bundle_root()


def is_portable() -> bool:
    """Portable (zip) builds ship a marker file next to the executable."""
    return IS_FROZEN and (install_dir() / "portable.txt").exists()


def ensure_dirs() -> None:
    for d in (user_data_dir(), output_dir(), local_data_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)
