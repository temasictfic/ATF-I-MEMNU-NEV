"""Main window placement: remembered between launches, always on screen."""

from __future__ import annotations

import pytest

from atfi_app.window_placement import Rect, WindowStateStore, compute_placement

LAPTOP = Rect(0, 0, 1920, 1032)  # 1920×1080 minus the taskbar


def _inside(r: Rect, area: Rect) -> bool:
    return r.x >= area.x and r.y >= area.y and r.right <= area.right and r.bottom <= area.bottom


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 1.75])
def test_first_launch_fits_the_screen(scale: float) -> None:
    p = compute_placement(None, [LAPTOP], LAPTOP, scale)
    assert _inside(p.bounds, LAPTOP)
    assert not p.maximized
    assert p.min_width <= LAPTOP.width and p.min_height <= LAPTOP.height
    # centered
    assert abs((p.bounds.x + p.bounds.right) / 2 - LAPTOP.width / 2) <= 1


def test_first_launch_at_100_percent_keeps_the_designed_size() -> None:
    big = Rect(0, 0, 2560, 1400)
    p = compute_placement(None, [big], big, 1.0)
    assert (p.bounds.width, p.bounds.height) == (1400, 900)


def test_saved_bounds_are_restored() -> None:
    saved = {"x": 100, "y": 50, "width": 1500, "height": 900, "maximized": False}
    p = compute_placement(saved, [LAPTOP], LAPTOP, 1.0)
    assert p.bounds == Rect(100, 50, 1500, 900)


def test_minimum_size_is_capped_to_the_screen() -> None:
    # 1000×700 at 150 % is 1500×1050: taller than this screen's work area.
    p = compute_placement(None, [LAPTOP], LAPTOP, 1.5)
    assert (p.min_width, p.min_height) == (1500, 1032)
    assert _inside(p.bounds, LAPTOP)


def test_saved_maximized_is_restored() -> None:
    saved = {"x": 100, "y": 50, "width": 1500, "height": 900, "maximized": True}
    assert compute_placement(saved, [LAPTOP], LAPTOP, 1.0).maximized


def test_saved_bounds_hanging_off_screen_are_pulled_back() -> None:
    saved = {"x": 1000, "y": 600, "width": 1500, "height": 900}
    p = compute_placement(saved, [LAPTOP], LAPTOP, 1.0)
    assert _inside(p.bounds, LAPTOP)
    assert (p.bounds.width, p.bounds.height) == (1500, 900)


def test_window_on_a_disconnected_monitor_comes_back_to_the_primary() -> None:
    saved = {"x": 2500, "y": 100, "width": 1200, "height": 800, "maximized": True}
    p = compute_placement(saved, [LAPTOP], LAPTOP, 1.0)
    assert _inside(p.bounds, LAPTOP)
    assert not p.maximized


def test_window_stays_on_its_secondary_monitor() -> None:
    second = Rect(1920, 0, 2560, 1400)
    saved = {"x": 2200, "y": 200, "width": 1400, "height": 900}
    p = compute_placement(saved, [LAPTOP, second], LAPTOP, 1.0)
    assert p.bounds == Rect(2200, 200, 1400, 900)


def test_garbage_state_falls_back_to_default() -> None:
    p = compute_placement({"x": "a"}, [LAPTOP], LAPTOP, 1.0)
    assert _inside(p.bounds, LAPTOP)


def test_store_round_trip(tmp_path) -> None:
    store = WindowStateStore(tmp_path / "window.json")
    assert store.load() is None
    store.save(Rect(10, 20, 1300, 800), maximized=True)
    assert store.load() == {"x": 10, "y": 20, "width": 1300, "height": 800, "maximized": True}
    (tmp_path / "window.json").write_text("{broken", encoding="utf-8")
    assert store.load() is None
