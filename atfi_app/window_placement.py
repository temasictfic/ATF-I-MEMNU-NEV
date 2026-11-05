"""Main window size and position: remembered between launches, kept on screen.

pywebview opens the window at a fixed logical size (1400×900, at least
1000×700) multiplied by the display scale and never checks it against the
screen. At 125–150 % scaling on a laptop that is larger than the screen, so
the window opened partly off screen, and it opened there again every time
because nothing was remembered.

This module is pure geometry (physical pixels, no CLR), so it is unit
tested on any OS; ``native/platform.py`` applies the result to the form.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger("atfi.window")

DEFAULT_LOGICAL_SIZE = (1400, 900)
MIN_LOGICAL_SIZE = (1000, 700)
# A fresh window never takes more than this share of the work area.
_DEFAULT_MAX_SHARE = 0.92
# A saved window is reused only if this much of it (each axis, physical px)
# is on some screen, so it can always be grabbed and moved.
_MIN_VISIBLE = 120


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def overlap(self, other: Rect) -> tuple[int, int]:
        w = min(self.right, other.right) - max(self.x, other.x)
        h = min(self.bottom, other.bottom) - max(self.y, other.y)
        return max(w, 0), max(h, 0)


@dataclass(frozen=True)
class Placement:
    bounds: Rect
    maximized: bool
    min_width: int
    min_height: int


def _fit(rect: Rect, area: Rect) -> Rect:
    """Shrink ``rect`` to fit ``area`` and move it fully inside."""
    width = min(rect.width, area.width)
    height = min(rect.height, area.height)
    x = min(max(rect.x, area.x), area.right - width)
    y = min(max(rect.y, area.y), area.bottom - height)
    return Rect(x, y, width, height)


def compute_placement(
    saved: dict[str, Any] | None,
    work_areas: list[Rect],
    primary: Rect,
    scale: float,
) -> Placement:
    """Where to open the window.

    ``work_areas`` are the screens' working areas (without the taskbar) and
    ``primary`` the primary screen's, all in physical pixels; ``scale`` is
    the display scale of the primary screen (1.0 = 96 DPI).
    """
    scale = scale if scale > 0 else 1.0
    target_area = primary
    bounds: Rect | None = None
    maximized = False

    if saved:
        try:
            candidate = Rect(int(saved["x"]), int(saved["y"]), int(saved["width"]), int(saved["height"]))
            maximized = bool(saved.get("maximized", False))
        except (KeyError, TypeError, ValueError):
            candidate = None
        if candidate is not None and candidate.width > 0 and candidate.height > 0:
            best = max(work_areas or [primary], key=lambda a: _area(candidate.overlap(a)))
            visible_w, visible_h = candidate.overlap(best)
            if visible_w >= min(_MIN_VISIBLE, candidate.width) and visible_h >= min(_MIN_VISIBLE, candidate.height):
                target_area = best
                bounds = candidate
            else:
                maximized = False  # the screen it was on is gone

    if bounds is None:
        width = min(round(DEFAULT_LOGICAL_SIZE[0] * scale), int(target_area.width * _DEFAULT_MAX_SHARE))
        height = min(round(DEFAULT_LOGICAL_SIZE[1] * scale), int(target_area.height * _DEFAULT_MAX_SHARE))
        bounds = Rect(
            target_area.x + (target_area.width - width) // 2,
            target_area.y + (target_area.height - height) // 2,
            width,
            height,
        )

    # The minimum size must fit the screen too, or Windows can't shrink the
    # window onto it.
    min_width = min(round(MIN_LOGICAL_SIZE[0] * scale), target_area.width)
    min_height = min(round(MIN_LOGICAL_SIZE[1] * scale), target_area.height)
    fitted = _fit(bounds, target_area)
    fitted = Rect(fitted.x, fitted.y, max(fitted.width, min_width), max(fitted.height, min_height))
    return Placement(_fit(fitted, target_area), maximized, min_width, min_height)


def _area(size: tuple[int, int]) -> int:
    return size[0] * size[1]


class WindowStateStore:
    """``window.json``: the last normal (restored) bounds and maximized flag."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def save(self, bounds: Rect, maximized: bool) -> None:
        data = {
            "x": bounds.x,
            "y": bounds.y,
            "width": bounds.width,
            "height": bounds.height,
            "maximized": maximized,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".window-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self._path)
        except OSError:
            logger.warning("could not save the window position", exc_info=True)
