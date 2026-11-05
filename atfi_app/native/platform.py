"""The desktop window: pywebview + the system WebView2 runtime.

``WebView2Platform.run`` replaces Electron's ``BrowserWindow`` and the
main-process IPC wiring. The main view loads the UI from the in-process
backend (``http://127.0.0.1:<port>/``) and is locked down to match the
Electron window, whose application menu was hidden:

* no browser accelerators (reload/print/find/zoom shortcuts),
* no zoom, pinch-zoom or swipe navigation,
* no default context menu,
* navigation restricted to the app origin; anything else opens in the
  system browser.

DevTools stay available on F12 / Ctrl+Shift+I, as in Electron (the page
handles the keys and calls ``openDevTools``).

The CLR-dependent modules are imported inside pywebview's ``before_show``
callback, which runs on the WinForms UI thread once pywebview has loaded
the CLR and the WebView2 assemblies.
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
import webbrowser
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..events import HostEvents
from ..window_placement import Placement, Rect, WindowStateStore, compute_placement

logger = logging.getLogger("atfi.native")

_SMOKE_TIMEOUT_S = 120


class _DropQueue:
    """Paths of files dropped on the page, handed over by WebView2.

    The page posts ``"FilesDropped"`` with the File objects attached
    (``postMessageWithAdditionalObjects``), then asks for the paths over
    HTTP. The two travel on different channels, so the request waits
    (bounded) until the paths have arrived.
    """

    def __init__(self) -> None:
        self._paths: deque[str] = deque()
        self._cond = threading.Condition()

    def push(self, paths: list[str]) -> None:
        with self._cond:
            self._paths.extend(paths)
            self._cond.notify_all()

    def take(self, count: int, timeout: float) -> list[str]:
        deadline = time.monotonic() + timeout
        with self._cond:
            while len(self._paths) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(remaining)
            n = min(count, len(self._paths))
            return [self._paths.popleft() for _ in range(n)]


class WebView2Platform:
    mode = "webview2"

    def __init__(self, *, events: HostEvents, smoke_test_path: str | None = None) -> None:
        self.events = events
        self._smoke_path = smoke_test_path
        self._window: Any = None
        self._form: Any = None
        self._main_ctrl: Any = None
        self._ui: Any = None
        self._guests: Any = None
        self._origin = ""
        self._core_ready = threading.Event()
        self._drops = _DropQueue()
        self._exit_code = 0
        self._on_shown: Callable[[], None] | None = None
        self._window_store: WindowStateStore | None = None
        self._placement: Placement | None = None

    # ---- lifecycle -------------------------------------------------------------

    def run(
        self,
        *,
        url: str,
        title: str,
        icon: Path,
        storage_path: Path,
        on_shown: Callable[[], None] | None = None,
        window_state_path: Path | None = None,
    ) -> int:
        import webview  # pywebview

        self._origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        self._on_shown = on_shown
        self._window_store = WindowStateStore(window_state_path) if window_state_path else None
        storage_path.mkdir(parents=True, exist_ok=True)
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["ALLOW_DOWNLOADS"] = False

        window = webview.create_window(
            title,
            url=url,
            width=1400,
            height=900,
            min_size=(1000, 700),
            background_color="#FFFFFF",
            text_select=True,  # pywebview disables text selection by default
            zoomable=False,
        )
        self._window = window
        window.events.before_show += self._on_before_show
        window.events.shown += self._on_window_shown
        webview.start(
            gui="edgechromium",
            private_mode=False,
            storage_path=str(storage_path),
            icon=str(icon) if icon.exists() else None,
            debug=False,
        )
        return self._exit_code

    def _on_before_show(self, window: Any) -> None:
        # UI thread; the CLR and WebView2 assemblies are loaded now.
        from .ui_thread import UiThread

        self._form = window.native
        self._main_ctrl = self._form.browser.webview
        self._ui = UiThread(self._form)
        self._main_ctrl.CoreWebView2InitializationCompleted += self._on_main_core_ready
        try:
            self._apply_placement()
        except Exception:  # noqa: BLE001  (never block startup on window geometry)
            logger.exception("could not place the window")
        self._form.Shown += self._on_form_shown
        self._form.FormClosing += self._on_form_closing

    # ---- window size and position (UI thread) --------------------------------

    def _screen_work_areas(self) -> tuple[list[Rect], Rect]:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

        def rect(r: Any) -> Rect:
            return Rect(int(r.X), int(r.Y), int(r.Width), int(r.Height))

        areas = [rect(screen.WorkingArea) for screen in WinForms.Screen.AllScreens]
        return areas, rect(WinForms.Screen.PrimaryScreen.WorkingArea)

    def _apply_placement(self) -> None:
        """Open at the remembered size and position, kept on a screen.

        pywebview sizes the window as 1400×900 logical pixels times the display
        scale (minimum 1000×700) without looking at the screen, which at
        125-150 % is larger than a laptop screen.
        """
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]
        from System.Drawing import Rectangle, Size  # type: ignore[import-not-found]

        form = self._form
        saved = self._window_store.load() if self._window_store else None
        areas, primary = self._screen_work_areas()
        try:
            scale = float(form._scale)  # pywebview: DPI of the window / 96
        except Exception:  # noqa: BLE001
            scale = 1.0
        placement = compute_placement(saved, areas, primary, scale)
        b = placement.bounds
        form.StartPosition = WinForms.FormStartPosition.Manual
        form.MinimumSize = Size(placement.min_width, placement.min_height)
        form.Bounds = Rectangle(b.x, b.y, b.width, b.height)
        if placement.maximized:
            form.WindowState = WinForms.FormWindowState.Maximized
        self._placement = placement
        logger.info("window placement %s (scale %.2f, saved=%s)", placement, scale, saved is not None)

    def _on_form_shown(self, sender: Any, args: Any) -> None:
        # WinForms DPI auto-scaling can resize the form once more while it is
        # shown; put it back where it was placed.
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]
        from System.Drawing import Rectangle  # type: ignore[import-not-found]

        placement = self._placement
        form = self._form
        if placement is None or form.WindowState != WinForms.FormWindowState.Normal:
            return
        b = placement.bounds
        current = form.Bounds
        if (current.X, current.Y, current.Width, current.Height) != (b.x, b.y, b.width, b.height):
            logger.info("window resized while showing (%s); restoring %s", current, b)
            form.Bounds = Rectangle(b.x, b.y, b.width, b.height)

    def _on_form_closing(self, sender: Any, args: Any) -> None:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

        if getattr(args, "Cancel", False) or self._window_store is None:
            return
        try:
            form = self._form
            state = form.WindowState
            r = form.Bounds if state == WinForms.FormWindowState.Normal else form.RestoreBounds
            self._window_store.save(
                Rect(int(r.X), int(r.Y), int(r.Width), int(r.Height)),
                maximized=state == WinForms.FormWindowState.Maximized,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not save the window position")

    def _on_window_shown(self) -> None:
        if self._on_shown is not None:
            self._on_shown()
        if self._smoke_path:
            threading.Thread(target=self._run_smoke_test, name="smoke-test", daemon=True).start()

    def _on_main_core_ready(self, sender: Any, args: Any) -> None:
        if not args.IsSuccess:
            logger.error("WebView2 failed to initialize: %s", args.InitializationException)
            self.show_error(
                "WebView2",
                "The Microsoft Edge WebView2 Runtime could not be started.\n\n"
                f"{args.InitializationException}\n\n"
                "Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 and restart the app.",
            )
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2WebResourceContext  # type: ignore[import-not-found]

        from .guest_view import GuestViewManager

        core = self._main_ctrl.CoreWebView2
        settings = core.Settings
        for name, value in (
            ("AreBrowserAcceleratorKeysEnabled", False),
            ("AreDefaultContextMenusEnabled", False),
            ("AreDevToolsEnabled", True),
            ("IsZoomControlEnabled", False),
            ("IsPinchZoomEnabled", False),
            ("IsSwipeNavigationEnabled", False),
            ("IsStatusBarEnabled", False),
            ("IsGeneralAutofillEnabled", False),
            ("IsPasswordAutosaveEnabled", False),
        ):
            try:
                setattr(settings, name, value)
            except Exception:  # noqa: BLE001  (older runtimes lack some settings)
                logger.debug("WebView2 setting %s unavailable", name)

        # pywebview routes *every* request of the main view through Python
        # (a '*' WebResourceRequested filter, for its request/response
        # events). Nothing here uses those events, so drop the filter; each
        # REST call no longer round-trips through the UI thread.
        try:
            core.RemoveWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All)
        except Exception:  # noqa: BLE001
            logger.debug("could not remove pywebview resource filter", exc_info=True)

        core.NavigationStarting += self._on_main_navigation_starting
        core.DocumentTitleChanged += self._on_title_changed
        core.WebMessageReceived += self._on_main_web_message
        core.ProcessFailed += self._on_process_failed

        self._guests = GuestViewManager(self._form, self._main_ctrl, self.events, self._ui)
        self._core_ready.set()

    # ---- main view event handlers (UI thread) --------------------------------

    def _on_main_navigation_starting(self, sender: Any, args: Any) -> None:
        uri = str(args.Uri)
        if uri.startswith(self._origin) or uri.startswith(("about:", "data:")):
            return
        args.Cancel = True
        if uri.startswith(("http://", "https://", "mailto:")):
            webbrowser.open(uri)

    def _on_title_changed(self, sender: Any, args: Any) -> None:
        title = str(self._main_ctrl.CoreWebView2.DocumentTitle or "")
        if title:
            self._form.Text = title

    def _on_main_web_message(self, sender: Any, args: Any) -> None:
        try:
            if args.TryGetWebMessageAsString() != "FilesDropped":
                return
        except Exception:  # noqa: BLE001  (JSON, not a string)
            return
        paths: list[str] = []
        objects = args.AdditionalObjects
        if objects is not None:
            for obj in objects:
                path = getattr(obj, "Path", None)
                if path:
                    paths.append(str(path))
        self._drops.push(paths)

    def _on_process_failed(self, sender: Any, args: Any) -> None:
        logger.error("WebView2 process failed: %s", args.ProcessFailedKind)
        if "BrowserProcessExited" in str(args.ProcessFailedKind) or "RenderProcessExited" in str(args.ProcessFailedKind):
            try:
                self._main_ctrl.CoreWebView2.Reload()
            except Exception:  # noqa: BLE001
                logger.exception("Reload after process failure failed")

    # ---- Platform API (worker threads) ---------------------------------------

    def _wait_ready(self) -> None:
        if not self._core_ready.wait(60):
            raise RuntimeError("WebView2 is not ready")

    def select_directory(self, default_path: str | None, title: str) -> str | None:
        from . import dialogs

        self._wait_ready()
        return self._ui.invoke(lambda: dialogs.select_directory(self._form, default_path, title))

    def select_pdfs(self, title: str) -> list[str]:
        from . import dialogs

        self._wait_ready()
        return self._ui.invoke(lambda: dialogs.select_pdfs(self._form, title))

    def save_as(self, title, default_path, filters, button_label):  # noqa: ANN001
        from . import dialogs

        self._wait_ready()
        return self._ui.invoke(lambda: dialogs.save_as(self._form, title, default_path, filters))

    def take_dropped_paths(self, count: int, timeout: float) -> list[str]:
        return self._drops.take(count, timeout)

    def open_devtools(self) -> None:
        self._wait_ready()
        self._ui.post(lambda: self._main_ctrl.CoreWebView2.OpenDevToolsWindow())

    def clear_scholar_session(self) -> None:
        self._wait_ready()
        self._guests.clear_scholar_session()

    def webview_create(self, view_id: str, options: dict[str, Any]) -> None:
        self._wait_ready()
        self._guests.create(view_id, options)

    def webview_destroy(self, view_id: str) -> None:
        if self._guests is not None:
            self._guests.destroy(view_id)

    def webview_set_bounds(self, view_id: str, bounds: dict[str, Any]) -> None:
        if self._guests is not None:
            self._guests.set_bounds(view_id, bounds)

    def webview_call(self, view_id: str, method: str, args: list[Any]) -> Any:
        self._wait_ready()
        return self._guests.call(view_id, method, args)

    def request_quit(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def activate_window(self) -> None:
        if self._ui is None:
            return

        def bring_to_front() -> None:
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            if self._form.WindowState == WinForms.FormWindowState.Minimized:
                self._form.WindowState = WinForms.FormWindowState.Normal
            self._form.Show()
            self._form.Activate()

        self._ui.post(bring_to_front)

    def show_error(self, title: str, message: str) -> None:
        # Plain Win32 message box: works before pywebview/CLR are loaded.
        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR)

    # ---- CI smoke test ---------------------------------------------------------

    def _run_smoke_test(self) -> None:
        """--smoke-test: assert the UI mounted, the guest-view glue works, screenshot, exit."""
        deadline = time.monotonic() + _SMOKE_TIMEOUT_S
        try:
            self._wait_ready()
            while time.monotonic() < deadline:
                mounted = self._window.evaluate_js(
                    "(() => { const a = document.getElementById('app'); return !!a && a.children.length > 0 })()"
                )
                if mounted and self._guests is not None and self._guests.views:
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("UI or hidden Scholar view did not come up")

            # Exercise the native guest path the Scholar scanner depends on.
            view_id = next(iter(self._guests.views))
            value = self._guests.call(
                view_id, "executeJavaScript", ["(async () => { await new Promise(r => setTimeout(r, 50)); return 6 * 7 })()"]
            )
            if value != 42:
                raise AssertionError(f"guest executeJavaScript returned {value!r}")

            self._check_window_on_screen()
            self._capture_screenshot(Path(self._smoke_path))
            logger.info("Smoke test passed")
            self._exit_code = 0
        except Exception:  # noqa: BLE001
            logger.exception("Smoke test failed")
            self._exit_code = 1
        finally:
            self.request_quit()

    def _check_window_on_screen(self) -> None:
        def measure() -> tuple[Any, Any]:
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            return self._form.Bounds, WinForms.Screen.FromControl(self._form).WorkingArea

        bounds, area = self._ui.invoke(measure)
        logger.info("window bounds %s, screen working area %s", bounds, area)
        if not (
            bounds.X >= area.X
            and bounds.Y >= area.Y
            and bounds.X + bounds.Width <= area.X + area.Width
            and bounds.Y + bounds.Height <= area.Y + area.Height
        ):
            raise AssertionError(f"window {bounds} is not inside the screen working area {area}")

    def _capture_screenshot(self, target: Path) -> None:
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat  # type: ignore[import-not-found]
        from System.IO import FileMode, FileStream  # type: ignore[import-not-found]

        from .ui_thread import wait_task

        target.parent.mkdir(parents=True, exist_ok=True)
        holder: dict[str, Any] = {}

        def start() -> Any:
            holder["fs"] = FileStream(os.fspath(target), FileMode.Create)
            return self._main_ctrl.CoreWebView2.CapturePreviewAsync(
                CoreWebView2CapturePreviewImageFormat.Png, holder["fs"]
            )

        task = self._ui.invoke(start)
        try:
            wait_task(task, 30)
        finally:
            self._ui.invoke(lambda: holder["fs"].Close())
