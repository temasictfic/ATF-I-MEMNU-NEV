"""Helpers for reading effective search runtime settings."""

import aiohttp
from config import settings as app_config

from services.settings_store import get_current_settings


def get_search_timeout_seconds() -> int:
    """Return effective per-search timeout in seconds from persisted settings."""
    try:
        configured = int(get_current_settings().search_timeout)
    except Exception:  # noqa: BLE001
        configured = int(app_config.search_timeout)
    return max(5, min(configured, 300))


def get_max_concurrent_apis() -> int:
    """Return effective API concurrency limit from persisted settings."""
    try:
        configured = int(get_current_settings().max_concurrent_apis)
    except Exception:  # noqa: BLE001
        configured = int(app_config.max_concurrent_apis)
    return max(1, min(configured, 50))


def get_max_concurrent_sources_per_pdf() -> int:
    """Return effective source-level concurrency limit for one PDF."""
    try:
        configured = int(get_current_settings().max_concurrent_sources_per_pdf)
    except Exception:  # noqa: BLE001
        configured = int(app_config.max_concurrent_sources_per_pdf)
    return max(1, min(configured, 20))


def get_polite_pool_email() -> str | None:
    """Return the configured polite-pool contact email (or None).

    Reads the dedicated ``polite_pool_email`` field.
    Returns ``None`` when neither is set so callers can decide
    whether to drop the mailto entirely instead of sending a fake address.
    """
    try:
        s = get_current_settings()
    except Exception:  # noqa: BLE001
        return None
    email = (s.polite_pool_email or "").strip()
    return email or None


def get_client_timeout(multiplier: float = 1.0) -> aiohttp.ClientTimeout:
    """Build an aiohttp timeout using the effective search timeout."""
    total = round(get_search_timeout_seconds() * max(multiplier, 0.1))
    return aiohttp.ClientTimeout(total=max(5, min(total, 300)))
