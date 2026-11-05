"""Atf-ı Memnu desktop host.

Replaces the Electron main process. It hosts the React UI in the system
WebView2 runtime (via pywebview), runs the FastAPI backend in-process, and
provides native services (dialogs, files, updater, Scholar browser views).
"""

from .version import __version__

__all__ = ["__version__"]
