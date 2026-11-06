"""Native WebView2 views behind the ``<atfi-webview>`` element.

These replace Electron's ``<webview>`` guests: the Scholar overlay browser
and the hidden Scholar scanner. Each is a
``Microsoft.Web.WebView2.WinForms.WebView2`` control placed on the main form
above the main UI view. It is created from the *same* CoreWebView2
environment and, for ``partition="persist:scholar-panel"``, the same
persistent ``scholar-panel`` profile. So, as with Electron's shared
partition, cookies from a CAPTCHA solved in the overlay are seen by the
scanner.

Compared with the Electron implementation:

* No user-agent / client-hint spoofing and no fingerprint-scrubbing preload.
  WebView2 *is* Microsoft Edge, so the UA, ``navigator.userAgentData``,
  ``Sec-CH-UA`` and ``navigator.webdriver`` are already consistent. Posing
  as Chrome would only create a mismatch.
* ``executeJavaScript`` uses DevTools ``Runtime.evaluate`` with
  ``awaitPromise``, matching Electron's semantics. ``ExecuteScriptAsync``
  doesn't await promises, and the Scholar scripts are async functions.
* Shortcuts pressed inside the guest (Ctrl+F, Esc, F5, Ctrl±0, Alt+←/→)
  are caught natively (``AcceleratorKeyPressed``) and sent to the page as
  ``guest-shortcut`` events. They no longer go through an injected
  ``console.log`` channel that any website could spoof.
  ``IsWebMessageEnabled`` is off, so ``chrome.webview`` is never exposed to
  third-party pages.
* Tracker requests (analytics/ad hosts) are blocked, scoped with WebView2
  resource filters so other requests never round-trip through Python.
  Scholar's own images/fonts/media load normally: a session that never
  fetches static assets is itself a bot signal.
"""

from __future__ import annotations

import ctypes
import json
import logging
import threading
from typing import Any

from Microsoft.Web.WebView2.Core import (  # type: ignore[import-not-found]
    CoreWebView2KeyEventKind,
    CoreWebView2WebErrorStatus,
    CoreWebView2WebResourceContext,
)
from Microsoft.Web.WebView2.WinForms import WebView2  # type: ignore[import-not-found]
from System.Drawing import Point, Rectangle, Region, Size  # type: ignore[import-not-found]
from System.Reflection import BindingFlags  # type: ignore[import-not-found]
from System.Windows.Forms import Timer  # type: ignore[import-not-found]

from ..events import HostEvents
from .ui_thread import UiThread, wait_task

logger = logging.getLogger("atfi.native.guest")

SCHOLAR_PROFILE = "scholar-panel"
MIN_ZOOM, MAX_ZOOM = 0.5, 3.0  # frontend/src/lib/constants/timings.ts
BACKGROUND_SIZE = (1280, 800)  # CSS px, as the Electron hidden scanner
DISPOSE_DELAY_MS = 1500  # a closed view stays hidden this long before it is disposed

TRACKER_HOST_PATTERNS = (
    "*google-analytics.com/*",
    "*googletagmanager.com/*",
    "*doubleclick.net/*",
    "*googleadservices.com/*",
)

# Mouse thumb buttons navigate the guest's history (Electron's guest did this
# natively; WebView2 does not by default).
_MOUSE_NAV_SCRIPT = """
(() => {
  if (window.__atfiMouseNav) return; window.__atfiMouseNav = true;
  document.addEventListener('mouseup', (e) => {
    if (e.button === 3) { e.preventDefault(); history.back(); }
    else if (e.button === 4) { e.preventDefault(); history.forward(); }
  }, true);
})();
"""

_VK_CONTROL, _VK_MENU, _VK_SHIFT = 0x11, 0x12, 0x10


def _key_down(vk: int) -> bool:
    return bool(ctypes.windll.user32.GetKeyState(vk) & 0x8000)


def _shortcut_for(vk: int) -> tuple[str, bool] | None:
    """Map an accelerator key to (action, swallow)."""
    ctrl, alt, shift = _key_down(_VK_CONTROL), _key_down(_VK_MENU), _key_down(_VK_SHIFT)
    if vk == 0x1B and not (ctrl or alt):  # Esc: still delivered to the page
        return ("escape", False)
    if vk == 0x74 and not alt:  # F5
        return ("reload", True)
    if vk == 0xA6:
        return ("back", True)  # VK_BROWSER_BACK
    if vk == 0xA7:
        return ("forward", True)  # VK_BROWSER_FORWARD
    if alt and not ctrl and vk == 0x25:
        return ("back", True)
    if alt and not ctrl and vk == 0x27:
        return ("forward", True)
    if ctrl and not alt:
        if vk == 0x46 and not shift:
            return ("find", True)
        if vk in (0xBB, 0x6B):
            return ("zoom-in", True)
        if vk in (0xBD, 0x6D):
            return ("zoom-out", True)
        if vk in (0x30, 0x60):
            return ("zoom-reset", True)
    return None


def _error_code(status: Any) -> int:
    # Chromium net error numbers, as Electron's did-fail-load reported them.
    name = str(status)
    if name == str(CoreWebView2WebErrorStatus.OperationCanceled):
        return -3  # ERR_ABORTED
    if name == str(CoreWebView2WebErrorStatus.Timeout):
        return -7
    if name == str(CoreWebView2WebErrorStatus.HostNameNotResolved):
        return -105
    if name == str(CoreWebView2WebErrorStatus.ConnectionAborted):
        return -103
    return -2  # ERR_FAILED


class GuestView:
    def __init__(self, manager: GuestViewManager, view_id: str, options: dict[str, Any]) -> None:
        self.manager = manager
        self.id = view_id
        self.background = bool(options.get("background"))
        self.allow_popups = bool(options.get("allowPopups"))
        partition = str(options.get("partition") or "")
        self.profile = SCHOLAR_PROFILE if "scholar" in partition else None
        self._initial_src = str(options.get("src") or "about:blank")
        self.ready = threading.Event()
        self.failed: str | None = None
        self.core: Any = None
        self._find_request_id = 0
        self._last_bounds: tuple[int, int, int, int] | None = None
        self.closed = False
        self._dispose_timer: Any = None

        ctrl = WebView2()
        self.ctrl = ctrl
        ctrl.Visible = False
        form = manager.form
        form.Controls.Add(ctrl)
        ctrl.BringToFront()
        if self.background:
            scale = manager.scale()
            ctrl.Location = Point(-20000, 0)
            ctrl.Size = Size(int(BACKGROUND_SIZE[0] * scale), int(BACKGROUND_SIZE[1] * scale))
            ctrl.Visible = True  # off-screen but "visible", so timers aren't throttled

        env = manager.environment
        controller_options = env.CreateCoreWebView2ControllerOptions()
        if self.profile:
            controller_options.ProfileName = self.profile
        controller_options.IsInPrivateModeEnabled = False
        ctrl.CoreWebView2InitializationCompleted += self._on_initialized
        ctrl.EnsureCoreWebView2Async(env, controller_options)

    # ---- setup ---------------------------------------------------------------

    def _on_initialized(self, sender: Any, args: Any) -> None:
        if not args.IsSuccess:
            self.failed = str(args.InitializationException)
            logger.error("Guest view %s failed to initialize: %s", self.id, self.failed)
            self.ready.set()
            return
        if self.closed:  # closed before it finished initializing
            self.failed = "webview was closed"
            self.ready.set()
            return
        core = self.ctrl.CoreWebView2
        self.core = core
        settings = core.Settings
        settings.IsWebMessageEnabled = False
        settings.AreBrowserAcceleratorKeysEnabled = False  # we route shortcuts ourselves
        settings.AreDevToolsEnabled = True
        settings.IsZoomControlEnabled = True  # native Ctrl+wheel; clamped below
        settings.IsStatusBarEnabled = True
        settings.AreDefaultScriptDialogsEnabled = True
        # No Edge autofill / save-password popups. On Google's sign-in page
        # the autofill popup attaches to this view, and closing the overlay
        # (Esc) while it was being torn down crashed the shared browser
        # process (access violation in msedge.dll), which blacked out the
        # whole app. Sign-in still persists through the profile's cookies.
        for name in ("IsGeneralAutofillEnabled", "IsPasswordAutosaveEnabled"):
            try:
                setattr(settings, name, False)
            except Exception:  # noqa: BLE001  (older runtimes lack some settings)
                logger.debug("WebView2 setting %s unavailable", name)

        core.ContentLoading += self._on_content_loading
        core.NavigationStarting += lambda s, e: self._emit("did-start-loading")
        core.NavigationCompleted += self._on_navigation_completed
        core.DOMContentLoaded += lambda s, e: self._emit("dom-ready")
        core.SourceChanged += self._on_source_changed
        core.HistoryChanged += lambda s, e: self._emit("history-changed")
        core.NewWindowRequested += self._on_new_window
        self.ctrl.ZoomFactorChanged += self._on_zoom_changed
        self._hook_accelerators()
        self._hook_find()
        if self.profile == SCHOLAR_PROFILE:
            self._install_resource_filters(core)
        core.AddScriptToExecuteOnDocumentCreatedAsync(_MOUSE_NAV_SCRIPT)

        self.ready.set()
        try:
            core.Navigate(self._initial_src)
        except Exception:  # noqa: BLE001
            logger.warning("Initial navigation to %s failed", self._initial_src, exc_info=True)

    def _hook_accelerators(self) -> None:
        try:
            field = self.ctrl.GetType().GetField(
                "_coreWebView2Controller", BindingFlags.NonPublic | BindingFlags.Instance
            )
            controller = field.GetValue(self.ctrl) if field is not None else None
            if controller is None:
                raise RuntimeError("controller field not found")
            controller.AcceleratorKeyPressed += self._on_accelerator
        except Exception:  # noqa: BLE001
            logger.warning("Guest shortcut handling unavailable", exc_info=True)

    def _hook_find(self) -> None:
        try:
            find = self.core.Find
            find.MatchCountChanged += lambda s, e: self._emit_find()
            find.ActiveMatchIndexChanged += lambda s, e: self._emit_find()
        except Exception:  # noqa: BLE001
            logger.warning("WebView2 Find API unavailable (runtime too old?)", exc_info=True)

    def _install_resource_filters(self, core: Any) -> None:
        for pattern in TRACKER_HOST_PATTERNS:
            core.AddWebResourceRequestedFilter(pattern, CoreWebView2WebResourceContext.All)
        core.WebResourceRequested += self._on_resource_requested

    # ---- events --------------------------------------------------------------

    def _state(self) -> dict[str, Any]:
        core = self.core
        if core is None:
            return {}
        try:
            return {
                "url": str(core.Source),
                "canGoBack": bool(core.CanGoBack),
                "canGoForward": bool(core.CanGoForward),
                "zoomFactor": float(self.ctrl.ZoomFactor),
            }
        except Exception:  # noqa: BLE001
            return {}

    def _emit(self, event_type: str, **fields: Any) -> None:
        if self.closed:
            return  # the page's element is gone; e.g. the about:blank teardown
        self.manager.events.publish(
            "webview", {"id": self.id, "type": event_type, "state": self._state(), **fields}
        )

    def _on_content_loading(self, sender: Any, args: Any) -> None:
        if not args.IsErrorPage:
            self._emit("did-navigate", url=str(self.core.Source))

    def _on_navigation_completed(self, sender: Any, args: Any) -> None:
        # WebView2 reports IsSuccess=False for HTTP 4xx/5xx responses too,
        # while Electron's did-fail-load fired only for network-level
        # failures. Google serves its CAPTCHA ("sorry") page as HTTP 429, so
        # treating that as a failed load made the Scholar scanner skip the
        # source instead of showing the CAPTCHA. A response with an HTTP
        # status is a page that loaded; only report real load failures.
        try:
            http_status = int(getattr(args, "HttpStatusCode", 0) or 0)
        except Exception:  # noqa: BLE001
            http_status = 0
        if not args.IsSuccess and http_status == 0:
            self._emit(
                "did-fail-load",
                errorCode=_error_code(args.WebErrorStatus),
                errorDescription=str(args.WebErrorStatus),
                validatedURL=str(self.core.Source),
                isMainFrame=True,
            )
        self._emit("did-stop-loading")

    def _on_source_changed(self, sender: Any, args: Any) -> None:
        if not args.IsNewDocument:
            self._emit("did-navigate-in-page", url=str(self.core.Source), isMainFrame=True)

    def _on_new_window(self, sender: Any, args: Any) -> None:
        args.Handled = True
        uri = str(args.Uri)
        if self.allow_popups and uri.startswith(("http://", "https://")):
            # Electron opened a separate bare window for popups. Opening the
            # link in the same panel keeps Back/Forward and the panel's
            # zoom/find tools working.
            self.core.Navigate(uri)

    def _on_zoom_changed(self, sender: Any, args: Any) -> None:
        factor = float(self.ctrl.ZoomFactor)
        clamped = min(MAX_ZOOM, max(MIN_ZOOM, factor))
        if abs(clamped - factor) > 1e-6:
            self.ctrl.ZoomFactor = clamped
            return  # the clamped value raises another ZoomFactorChanged
        self._emit("zoom-changed", zoomFactor=factor)

    def _on_accelerator(self, sender: Any, args: Any) -> None:
        if args.KeyEventKind not in (CoreWebView2KeyEventKind.KeyDown, CoreWebView2KeyEventKind.SystemKeyDown):
            return
        if args.PhysicalKeyStatus.WasKeyDown:
            return  # auto-repeat
        mapped = _shortcut_for(int(args.VirtualKey))
        if mapped is None:
            return
        action, swallow = mapped
        if swallow:
            args.Handled = True
        self._emit("guest-shortcut", action=action)

    def _emit_find(self) -> None:
        try:
            find = self.core.Find
            matches = int(find.MatchCount)
            active = int(find.ActiveMatchIndex)
        except Exception:  # noqa: BLE001
            return
        result = {
            "requestId": self._find_request_id,
            "matches": max(0, matches),
            "activeMatchOrdinal": active + 1 if active >= 0 else 0,
            "finalUpdate": True,
        }
        self._emit("found-in-page", result=result)

    def _on_resource_requested(self, sender: Any, args: Any) -> None:
        uri = str(args.Request.Uri)
        blocked = any(host in uri for host in ("google-analytics.com", "googletagmanager.com",
                                               "doubleclick.net", "googleadservices.com"))
        if blocked:
            args.Response = self.core.Environment.CreateWebResourceResponse(None, 403, "Blocked", "")

    # ---- operations (UI thread) ----------------------------------------------

    def set_bounds(self, bounds: dict[str, Any]) -> None:
        if self.background or self.closed:
            return
        rect = bounds.get("rect") or None
        visible = bool(bounds.get("visible")) and rect is not None
        if not visible:
            self.ctrl.Visible = False
            return
        dpr = float(bounds.get("dpr") or 1.0)
        x, y = round(rect["x"] * dpr), round(rect["y"] * dpr)
        w, h = max(1, round(rect["width"] * dpr)), max(1, round(rect["height"] * dpr))
        if (x, y, w, h) != self._last_bounds:
            self.ctrl.Bounds = Rectangle(x, y, w, h)
            self._last_bounds = (x, y, w, h)
        holes = bounds.get("holes") or []
        if holes:
            region = Region(Rectangle(0, 0, w, h))
            for hole in holes:
                region.Exclude(
                    Rectangle(
                        round(hole["x"] * dpr), round(hole["y"] * dpr),
                        round(hole["width"] * dpr), round(hole["height"] * dpr),
                    )
                )
            self.ctrl.Region = region
        elif self.ctrl.Region is not None:
            self.ctrl.Region = None
        if not self.ctrl.Visible:
            self.ctrl.Visible = True
            self.ctrl.BringToFront()

    def destroy(self) -> None:
        """Hide the view now and dispose it once the browser has let go of it.

        Disposing the controller right away, while the browser process is
        still handling the key press that closed the overlay (or closing a
        popup anchored to this view), can crash the shared browser process
        and take every view down with it. Hiding the view and moving it to
        about:blank first lets that work finish against a live window.
        """
        if self.closed:
            return
        self.closed = True
        try:
            self.ctrl.Visible = False
        except Exception:  # noqa: BLE001
            logger.debug("guest hide failed", exc_info=True)
        if self.core is not None:
            try:
                self.core.Stop()
                self.core.Navigate("about:blank")
            except Exception:  # noqa: BLE001
                logger.debug("guest blank navigation failed", exc_info=True)
        timer = Timer()
        timer.Interval = DISPOSE_DELAY_MS
        self._dispose_timer = timer  # keep a reference until it fires

        def fire(sender: Any, args: Any) -> None:
            timer.Stop()
            timer.Dispose()
            self._dispose_timer = None
            self._dispose()

        timer.Tick += fire
        timer.Start()

    def _dispose(self) -> None:
        try:
            if not self.manager.form.IsDisposed:
                self.manager.form.Controls.Remove(self.ctrl)
            self.ctrl.Dispose()
        except Exception:  # noqa: BLE001
            logger.debug("guest dispose failed", exc_info=True)
        self.manager.closing.discard(self)

    def call_ui(self, method: str, args: list[Any]) -> Any:
        """Synchronous operations; runs on the UI thread once the view is ready."""
        core = self.core
        if core is None:
            raise RuntimeError(self.failed or "webview is not ready")
        if method == "loadURL":
            core.Navigate(str(args[0]))
        elif method == "goBack":
            if core.CanGoBack:
                core.GoBack()
        elif method == "goForward":
            if core.CanGoForward:
                core.GoForward()
        elif method == "reload":
            core.Reload()
        elif method == "stop":
            core.Stop()
        elif method == "setZoomFactor":
            self.ctrl.ZoomFactor = min(MAX_ZOOM, max(MIN_ZOOM, float(args[0])))
        elif method == "findInPage":
            self._find(str(args[0]), args[1] if len(args) > 1 and isinstance(args[1], dict) else {})
        elif method == "stopFindInPage":
            try:
                core.Find.Stop()
            except Exception:  # noqa: BLE001
                pass
        elif method == "openDevTools":
            core.OpenDevToolsWindow()
        else:
            raise ValueError(f"unsupported webview method {method!r}")
        return None

    def _find(self, text: str, options: dict[str, Any]) -> None:
        self._find_request_id = int(options.get("requestId") or self._find_request_id + 1)
        find = self.core.Find
        if not text:
            find.Stop()
            return
        if options.get("findNext"):
            if options.get("forward", True):
                find.FindNext()
            else:
                find.FindPrevious()
            return
        opts = self.core.Environment.CreateFindOptions()
        opts.FindTerm = text
        opts.IsCaseSensitive = bool(options.get("matchCase"))
        opts.SuppressDefaultFindDialog = True
        opts.ShouldHighlightAllMatches = True
        find.StartAsync(opts)


class GuestViewManager:
    def __init__(self, form: Any, main_ctrl: Any, events: HostEvents, ui: UiThread) -> None:
        self.form = form
        self.main_ctrl = main_ctrl
        self.events = events
        self.ui = ui
        self.views: dict[str, GuestView] = {}
        self.closing: set[GuestView] = set()  # hidden, waiting to be disposed

    @property
    def environment(self) -> Any:
        return self.main_ctrl.CoreWebView2.Environment

    def scale(self) -> float:
        try:
            return float(self.form.DeviceDpi) / 96.0
        except Exception:  # noqa: BLE001
            return 1.0

    # ---- RPC entry points (worker threads) -----------------------------------

    def create(self, view_id: str, options: dict[str, Any]) -> None:
        def make() -> None:
            old = self.views.pop(view_id, None)
            if old is not None:
                self.closing.add(old)
                old.destroy()
            self.views[view_id] = GuestView(self, view_id, options or {})

        self.ui.invoke(make)

    def destroy(self, view_id: str) -> None:
        def drop() -> None:
            view = self.views.pop(view_id, None)
            if view is not None:
                self.closing.add(view)
                view.destroy()

        self.ui.post(drop)

    def set_bounds(self, view_id: str, bounds: dict[str, Any]) -> None:
        def apply() -> None:
            view = self.views.get(view_id)
            if view is not None:
                view.set_bounds(bounds or {})

        self.ui.post(apply)

    def call(self, view_id: str, method: str, args: list[Any]) -> Any:
        view = self.views.get(view_id)
        if view is None:
            raise KeyError(f"unknown webview {view_id}")
        if not view.ready.wait(30):
            raise TimeoutError("webview did not initialize")
        if method == "executeJavaScript":
            return self._evaluate(view, str(args[0]) if args else "")
        return self.ui.invoke(lambda: view.call_ui(method, list(args or [])))

    def _evaluate(self, view: GuestView, expression: str, timeout: float = 30.0) -> Any:
        params = json.dumps(
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            }
        )
        task = self.ui.invoke(lambda: view.core.CallDevToolsProtocolMethodAsync("Runtime.evaluate", params))
        reply = json.loads(str(wait_task(task, timeout)))
        if reply.get("exceptionDetails"):
            details = reply["exceptionDetails"]
            message = (details.get("exception") or {}).get("description") or details.get("text") or "error"
            raise RuntimeError(f"Script error: {message}")
        return (reply.get("result") or {}).get("value")

    def clear_scholar_session(self) -> None:
        view = next((v for v in self.views.values() if v.profile == SCHOLAR_PROFILE and v.core), None)
        if view is None:
            raise RuntimeError("Scholar browser profile is not loaded yet")
        task = self.ui.invoke(lambda: view.core.Profile.ClearBrowsingDataAsync())
        wait_task(task, 60)
