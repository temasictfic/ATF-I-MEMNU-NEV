"""UTC-day request counter persisted to ``<output_dir>/quota_state.json``.

Pacing alone (the per-domain rate limiter) cannot enforce daily caps — Web
of Science Starter ranges from 50 req/day on the free tier to 20,000 on the
expanded tier, which a sustained verification session can hit before the
day ends. This module tracks per-key counters that reset at UTC midnight
(matching Clarivate's day boundary) and persists them so a process restart
mid-day doesn't reset the count and trick us into spending the cap twice.

Atomic writes via tmp + rename (mirrors :mod:`services.cache_store`). The
hot path only mutates the in-memory dict and sets a dirty flag; a small
background flush task writes to disk every ~5 s when dirty, so 5,000
verifier calls a day cost at most ~17,000 disk writes worth of work
collapsed into ~280 actual writes.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import settings as app_config

_STATE_FILENAME = "quota_state.json"
_FLUSH_INTERVAL_SECONDS = 5.0


def _utc_today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _seconds_until_utc_midnight() -> float:
    now = datetime.now(timezone.utc)
    tomorrow = now.date().toordinal() + 1
    next_midnight = datetime.fromordinal(tomorrow).replace(tzinfo=timezone.utc)
    return max(0.0, (next_midnight - now).total_seconds())


class DailyQuota:
    """Per-key UTC-day counter with atomic JSON persistence."""

    def __init__(self, state_path: Path):
        self._state_path = state_path
        self._lock = threading.Lock()
        self._date: str = _utc_today_iso()
        self._counters: dict[str, int] = {}
        self._dirty: bool = False
        self._flush_thread: threading.Thread | None = None
        self._stop_flush = threading.Event()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        stored_date = data.get("date")
        counters = data.get("counters") or {}
        if not isinstance(counters, dict):
            counters = {}
        today = _utc_today_iso()
        if stored_date == today:
            self._counters = {
                k: int(v) for k, v in counters.items() if isinstance(v, (int, float))
            }
        # Different day → counters reset to empty (already the default).
        self._date = today

    def _maybe_roll_date(self) -> None:
        today = _utc_today_iso()
        if today != self._date:
            self._date = today
            self._counters = {}
            self._dirty = True

    def consume(self, key: str, max_per_day: int) -> bool:
        """Consume one slot for ``key``. Returns False when the cap is reached."""
        with self._lock:
            self._maybe_roll_date()
            current = self._counters.get(key, 0)
            if current >= max_per_day:
                return False
            self._counters[key] = current + 1
            self._dirty = True
            return True

    def remaining(self, key: str, max_per_day: int) -> int:
        with self._lock:
            self._maybe_roll_date()
            return max(0, max_per_day - self._counters.get(key, 0))

    def used(self, key: str) -> int:
        with self._lock:
            self._maybe_roll_date()
            return self._counters.get(key, 0)

    def seconds_until_reset(self) -> float:
        return _seconds_until_utc_midnight()

    def flush(self) -> None:
        """Persist the state file if dirty. Atomic via tmp + rename."""
        with self._lock:
            if not self._dirty:
                return
            payload = {"date": self._date, "counters": dict(self._counters)}
            self._dirty = False
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, self._state_path)
        except OSError:
            with self._lock:
                self._dirty = True

    def start_background_flush(self) -> None:
        """Start the periodic flush thread. Idempotent."""
        if self._flush_thread is not None and self._flush_thread.is_alive():
            return
        self._stop_flush.clear()

        def _run() -> None:
            while not self._stop_flush.wait(_FLUSH_INTERVAL_SECONDS):
                try:
                    self.flush()
                except Exception:  # noqa: BLE001, S110
                    pass

        self._flush_thread = threading.Thread(
            target=_run, name="daily-quota-flush", daemon=True
        )
        self._flush_thread.start()

    def stop_background_flush(self) -> None:
        self._stop_flush.set()
        if self._flush_thread is not None:
            self._flush_thread.join(timeout=2.0)
            self._flush_thread = None
        self.flush()


def _state_path() -> Path:
    return Path(app_config.output_dir) / _STATE_FILENAME


daily_quota = DailyQuota(_state_path())
daily_quota.start_background_flush()
