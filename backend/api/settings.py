from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter
from models.settings import AppSettings, DatabaseConfig
from pydantic import BaseModel
from services.openaire_token_manager import (
    exchange_refresh_token,
    get_runtime_status,
    invalidate_cache,
)
from services.settings_store import get_current_settings, save_settings

router = APIRouter()

# Re-export the public accessor for any external caller still importing
# `get_current_settings` from `api.settings`. The canonical home is now
# `services.settings_store` — this module is just the FastAPI router.
__all__ = ["get_current_settings", "router"]


@router.get("/settings")
async def get_settings():
    s = get_current_settings()
    return s.model_dump()


@router.put("/settings")
async def update_settings(patch: dict[str, Any]):
    """Apply a partial update for scalar fields.

    The body is a JSON object whose keys are AppSettings field names —
    the renderer sends only the fields that actually changed. Unknown keys
    and fields that fail per-field Pydantic validation are silently
    dropped, so a stale-renderer race or a model rename can't wipe
    unrelated fields.

    Two fields are handled specially:

    - ``databases`` is rejected here — use the granular /settings/databases/*
      endpoints, which apply per-row operations to the current on-disk
      list and so cannot lose user reordering on a stale-renderer save.
    - ``api_keys`` is merged into the existing dict rather than replacing
      it. This means a stale-renderer PATCH carrying only the one key the
      user just typed can't drop the others. Keys explicitly listed in
      the patch (including with empty-string values) override; keys
      missing from the patch are preserved.
    """
    previous = get_current_settings()

    updates: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        if key == "databases":
            # Defence in depth — the renderer routes database edits to
            # the granular endpoints. Drop any here so a future caller
            # mistake can't replace the on-disk list.
            continue
        if key not in AppSettings.model_fields:
            continue
        try:
            AppSettings.model_validate({key: value})
        except Exception:  # noqa: BLE001, S112
            continue
        updates[key] = value

    if "api_keys" in updates:
        # Per-key merge: existing keys not listed in the patch survive,
        # listed keys take the incoming value (empty string clears).
        incoming_keys = updates["api_keys"] or {}
        updates["api_keys"] = {**previous.api_keys, **incoming_keys}

    if not updates:
        return previous.model_dump()

    new_settings = previous.model_copy(update=updates)

    # OpenAIRE cache invalidation: only fires when api_keys was in the
    # patch and the openaire entry actually changed.
    if "api_keys" in updates:
        try:
            if previous.api_keys.get("openaire", "") != new_settings.api_keys.get(
                "openaire", ""
            ):
                invalidate_cache()
        except Exception:  # noqa: BLE001, S110
            pass

    save_settings(new_settings)
    return new_settings.model_dump()


# ---------------------------------------------------------------------------
# Granular database endpoints. Each operation reads the current on-disk
# database list and applies a single, well-defined edit — never replaces
# the list wholesale. This lets a stale-renderer (one whose Zustand state
# is still the seed defaults because GET /settings hadn't yet resolved)
# safely toggle / reorder / add / remove a row without losing the user's
# real ordering or other rows.
# ---------------------------------------------------------------------------


class DatabaseToggleRequest(BaseModel):
    enabled: bool


class DatabaseReorderRequest(BaseModel):
    id: str
    # ``after_id`` is the id of the row that should sit immediately
    # before this one in the new order. ``None`` means place at the head
    # of the list. Using an id (not an index) keeps the operation
    # well-defined when the renderer's view of the list differs from
    # the backend's — e.g. after a stale render where rows were added
    # to the canonical list since the last GET.
    after_id: str | None = None


@router.put("/settings/databases/{db_id}")
async def update_database(db_id: str, req: DatabaseToggleRequest):
    previous = get_current_settings()
    dbs = list(previous.databases)
    for i, db in enumerate(dbs):
        if db.id == db_id:
            dbs[i] = db.model_copy(update={"enabled": req.enabled})
            new_settings = previous.model_copy(update={"databases": dbs})
            save_settings(new_settings)
            return new_settings.model_dump()
    return previous.model_dump()


@router.post("/settings/databases/reorder")
async def reorder_database(req: DatabaseReorderRequest):
    previous = get_current_settings()
    dbs = list(previous.databases)
    moved_idx = next((i for i, db in enumerate(dbs) if db.id == req.id), -1)
    if moved_idx < 0:
        return previous.model_dump()
    moved = dbs.pop(moved_idx)
    if req.after_id is None:
        dbs.insert(0, moved)
    else:
        after_idx = next((i for i, db in enumerate(dbs) if db.id == req.after_id), -1)
        if after_idx < 0:
            # Anchor row no longer exists — append to keep the move
            # observable rather than silently no-op'ing.
            dbs.append(moved)
        else:
            dbs.insert(after_idx + 1, moved)
    new_settings = previous.model_copy(update={"databases": dbs})
    save_settings(new_settings)
    return new_settings.model_dump()


@router.post("/settings/databases")
async def add_database(db: DatabaseConfig):
    previous = get_current_settings()
    if any(d.id == db.id for d in previous.databases):
        return previous.model_dump()
    new_dbs = [*previous.databases, db]
    new_settings = previous.model_copy(update={"databases": new_dbs})
    save_settings(new_settings)
    return new_settings.model_dump()


@router.delete("/settings/databases/{db_id}")
async def remove_database(db_id: str):
    previous = get_current_settings()
    new_dbs = [d for d in previous.databases if d.id != db_id]
    if len(new_dbs) == len(previous.databases):
        return previous.model_dump()
    new_settings = previous.model_copy(update={"databases": new_dbs})
    save_settings(new_settings)
    return new_settings.model_dump()


class OpenaireValidateRequest(BaseModel):
    refresh_token: str


@router.post("/settings/openaire/validate")
async def validate_openaire_token(payload: OpenaireValidateRequest):
    """Exchange the pasted refresh token against OpenAIRE and persist on success.

    This is the primary entry point for the "Connect" button. Keeping the
    refresh-token exchange on the server side means the renderer never has
    to deal with OpenAIRE's CORS stance, and validation + persistence are
    one atomic step: if the exchange fails, nothing is saved.
    """
    token = (payload.refresh_token or "").strip()
    if not token:
        return {"valid": False, "error": "Token is empty."}

    try:
        await exchange_refresh_token(token)
    except RuntimeError as exc:
        return {"valid": False, "error": str(exc)}

    current = get_current_settings()
    updated_keys = dict(current.api_keys)
    updated_keys["openaire"] = token
    updated = current.model_copy(update={
        "api_keys": updated_keys,
        "openaire_token_saved_at": datetime.now(timezone.utc).date().isoformat(),
    })
    save_settings(updated)
    invalidate_cache()

    return {"valid": True, "settings": updated.model_dump()}


@router.get("/settings/openaire/status")
async def get_openaire_status():
    """Expose the most recent refresh-token exchange outcome.

    Lets the Settings page surface silent mid-run auth failures (expired
    token, OpenAIRE brownout, etc.) that would otherwise go unnoticed —
    the verifier falls back to anonymous, so rates quietly tank without a
    visible error anywhere.
    """
    return get_runtime_status()


@router.post("/settings/openaire/disconnect")
async def disconnect_openaire():
    """Clear the stored OpenAIRE refresh token and reset its saved-at date."""
    current = get_current_settings()
    updated_keys = {k: v for k, v in current.api_keys.items() if k != "openaire"}
    updated = current.model_copy(update={
        "api_keys": updated_keys,
        "openaire_token_saved_at": "",
    })
    save_settings(updated)
    invalidate_cache()
    return updated.model_dump()
