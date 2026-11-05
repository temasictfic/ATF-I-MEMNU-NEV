"""Marshal work onto the WinForms UI thread.

WebView2 objects may only be touched on the thread that created them (the
pywebview STA GUI thread). Host RPCs arrive on uvicorn worker threads, so
they go through ``invoke`` (synchronous, propagates exceptions) or ``post``
(fire-and-forget).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from System import Action  # type: ignore[import-not-found]

logger = logging.getLogger("atfi.native.ui")

T = TypeVar("T")


class UiThread:
    def __init__(self, control: Any) -> None:
        self._control = control  # any WinForms control owned by the UI thread

    def invoke(self, fn: Callable[[], T]) -> T:
        if not self._control.InvokeRequired:
            return fn()
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                box["result"] = fn()
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        self._control.Invoke(Action(run))
        if "error" in box:
            raise box["error"]
        return box.get("result")  # type: ignore[return-value]

    def post(self, fn: Callable[[], Any]) -> None:
        def run() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.exception("UI task failed")

        if self._control.IsDisposed:
            return
        self._control.BeginInvoke(Action(run))


def wait_task(task: Any, timeout: float) -> Any:
    """Block a worker thread until a .NET Task completes; return its Result.

    WebView2 completes its tasks on the UI thread via the message loop, so
    this must never be called *on* the UI thread.
    """
    if not task.Wait(int(timeout * 1000)):
        raise TimeoutError("WebView2 operation timed out")
    if task.IsFaulted:
        raise RuntimeError(str(task.Exception.GetBaseException().Message))
    try:
        return task.Result
    except AttributeError:  # non-generic Task
        return None
