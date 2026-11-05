"""Interface between host services and the UI platform.

Two implementations:

* ``atfi_app.native.platform.WebView2Platform``: the real desktop app
  (pywebview window, WinForms dialogs, native WebView2 guest views). Windows only.
* ``DevBrowserPlatform`` (below): the UI is opened in an ordinary browser
  for development and automated UI tests on any OS. Dialogs come from
  environment variables and there are no native guest views (the
  ``<atfi-webview>`` element falls back to an iframe).
"""

from __future__ import annotations

import os
from typing import Any, Protocol


class Platform(Protocol):
    mode: str  # "webview2" | "dev-browser"

    def select_directory(self, default_path: str | None, title: str) -> str | None: ...
    def select_pdfs(self, title: str) -> list[str]: ...
    def save_as(self, title: str, default_path: str | None, filters: list[dict[str, Any]],
                button_label: str | None) -> str | None: ...
    def take_dropped_paths(self, count: int, timeout: float) -> list[str]: ...
    def open_devtools(self) -> None: ...
    def clear_scholar_session(self) -> None: ...
    def webview_create(self, view_id: str, options: dict[str, Any]) -> None: ...
    def webview_destroy(self, view_id: str) -> None: ...
    def webview_set_bounds(self, view_id: str, bounds: dict[str, Any]) -> None: ...
    def webview_call(self, view_id: str, method: str, args: list[Any]) -> Any: ...
    def request_quit(self) -> None: ...
    def activate_window(self) -> None: ...
    def show_error(self, title: str, message: str) -> None: ...


class DevBrowserPlatform:
    """Platform used by ``python -m atfi_app --dev-browser``.

    The native dialogs are scripted through environment variables so UI
    tests can drive imports and exports:

    ATFI_DEV_PICK_PDFS   os.pathsep-separated paths returned by "Import PDFs"
    ATFI_DEV_PICK_DIR    directory returned by folder pickers
    ATFI_DEV_SAVE_PATH   path returned by "Save as"
    """

    mode = "dev-browser"

    def __init__(self) -> None:
        self.quit_requested = False

    def select_directory(self, default_path: str | None, title: str) -> str | None:
        return os.environ.get("ATFI_DEV_PICK_DIR") or None

    def select_pdfs(self, title: str) -> list[str]:
        raw = os.environ.get("ATFI_DEV_PICK_PDFS", "")
        return [p for p in raw.split(os.pathsep) if p]

    def save_as(self, title, default_path, filters, button_label):  # noqa: ANN001
        return os.environ.get("ATFI_DEV_SAVE_PATH") or None

    def take_dropped_paths(self, count: int, timeout: float) -> list[str]:
        return []

    def open_devtools(self) -> None:
        pass

    def clear_scholar_session(self) -> None:
        pass

    def webview_create(self, view_id, options) -> None:  # noqa: ANN001
        raise RuntimeError("Native webviews are only available in the desktop app")

    def webview_destroy(self, view_id) -> None:  # noqa: ANN001
        pass

    def webview_set_bounds(self, view_id, bounds) -> None:  # noqa: ANN001
        pass

    def webview_call(self, view_id, method, args):  # noqa: ANN001
        raise RuntimeError("Native webviews are only available in the desktop app")

    def request_quit(self) -> None:
        self.quit_requested = True

    def activate_window(self) -> None:
        pass

    def show_error(self, title: str, message: str) -> None:
        import sys

        print(f"{title}: {message}", file=sys.stderr)
