"""Settings persistence and the in-memory current-settings cache.

Extracted from ``api.settings`` so ``services.openaire_token_manager`` (and
anyone else who needs to read or rotate settings) can import it without
forming the api/settings ↔ services/openaire_token_manager cycle.

The FastAPI router still lives in ``api.settings`` and delegates the
persistence + accessor calls here.
"""

import json
from pathlib import Path

from config import settings as app_config
from models.settings import AppSettings


_current_settings: AppSettings | None = None


def get_current_settings() -> AppSettings:
    """Public accessor for current settings (used by orchestrator/verifiers)."""
    if _current_settings is None:
        _load_settings()
    assert _current_settings is not None
    return _current_settings


def _load_settings() -> AppSettings:
    global _current_settings
    settings_path = app_config.get_settings_path()
    if not settings_path.exists():
        _current_settings = AppSettings.default()
        return _current_settings

    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Per-field load: one invalid/renamed field shouldn't wipe the rest of
    # the file across version upgrades (the UI's auto-save would otherwise
    # overwrite the good file with defaults on the next change).
    valid: dict = {}
    for key, value in data.items():
        if key not in AppSettings.model_fields:
            continue
        try:
            AppSettings.model_validate({key: value})
            valid[key] = value
        except Exception:  # noqa: BLE001, S112
            continue

    if not valid.get("exclusion_list_url"):
        valid["exclusion_list_url"] = AppSettings.default().exclusion_list_url
    if not valid.get("exclusion_edit_url"):
        valid["exclusion_edit_url"] = AppSettings.default().exclusion_edit_url

    try:
        _current_settings = AppSettings(**valid)
    except Exception:  # noqa: BLE001
        _current_settings = AppSettings.default()
    _current_settings = _migrate_databases(_current_settings)
    return _current_settings



def _migrate_databases(s: AppSettings) -> AppSettings:
    """Reconcile stored databases with current defaults.

    Preserves the user's chosen order and enabled state. New defaults are
    appended at the tail; entries no longer in defaults are dropped. The
    display name is refreshed from defaults so a renamed verifier picks up
    the new label on next load.
    """
    defaults = AppSettings.default()
    defaults_by_id = {db.id: db for db in defaults.databases}
    stored_ids = {db.id for db in s.databases}

    merged = []
    for db in s.databases:
        if db.id in defaults_by_id:
            merged.append(db.model_copy(update={"name": defaults_by_id[db.id].name}))

    for db in defaults.databases:
        if db.id not in stored_ids:
            merged.append(db)

    changed = s.databases != merged
    s.databases = merged

    # Keep runtime settings in a safe range even if a legacy settings file has bad values.
    timeout = int(s.search_timeout or defaults.search_timeout)
    concurrent = int(s.max_concurrent_apis or defaults.max_concurrent_apis)
    source_concurrent = int(
        s.max_concurrent_sources_per_pdf or defaults.max_concurrent_sources_per_pdf
    )
    sanitized_timeout = max(5, min(timeout, 300))
    sanitized_concurrent = max(1, min(concurrent, 50))
    sanitized_source_concurrent = max(1, min(source_concurrent, 20))

    if s.search_timeout != sanitized_timeout:
        s.search_timeout = sanitized_timeout
        changed = True

    if s.max_concurrent_apis != sanitized_concurrent:
        s.max_concurrent_apis = sanitized_concurrent
        changed = True

    if s.max_concurrent_sources_per_pdf != sanitized_source_concurrent:
        s.max_concurrent_sources_per_pdf = sanitized_source_concurrent
        changed = True

    if changed:
        save_settings(s)

    return s


def save_settings(s: AppSettings) -> None:
    global _current_settings
    settings_path = app_config.get_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(s.model_dump_json(indent=2), encoding="utf-8")
    _current_settings = s
