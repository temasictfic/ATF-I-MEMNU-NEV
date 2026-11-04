"""OpenAIRE refresh-token exchange and short-lived access-token cache.

Anonymous OpenAIRE is capped at 60 req/hour (one call per minute sliding
window) which is nowhere near enough for a 40-PDF batch. A registered user
can lift that cap to 7,200 req/hour by pasting a refresh token into Settings.

Refresh tokens expire 1 month after issuance; access tokens expire 1 hour.
We therefore:

- persist only the **refresh token** (in `settings.api_keys["openaire"]`),
- exchange it lazily for an access token on demand, cached in memory until
  60 s before expiry,
- if the exchange response rotates the refresh token (OpenAIRE's docs
  indicate this can happen), write the new one back to settings and bump
  `openaire_token_saved_at` so the UI warns at the correct time.

The verifier calls :func:`get_access_token` before each request and falls
back to anonymous if it returns ``None`` — the app stays usable even when
the user has not connected, has typo'd their token, or OpenAIRE is down.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Literal

import aiohttp

from services.settings_store import get_current_settings, save_settings
from verifiers._http import create_ssl_context

REFRESH_ENDPOINT = (
    "https://services.openaire.eu/uoa-user-management/api/users/getAccessToken"
)

# Safety margin: treat the cached access token as expired a minute before
# OpenAIRE claims, so we never emit a request with a token that dies mid-flight.
_EXPIRY_SAFETY_MARGIN_SECONDS = 60.0

_cache_lock = asyncio.Lock()
_cached_access_token: str | None = None
_cached_expires_at: float = 0.0  # monotonic seconds; 0 = "no cache"
# Truncated fingerprint of the refresh token the cached access token was
# derived from. Lets us detect out-of-band rotations (user edited settings,
# or a disconnect+reconnect with a different token) and drop the stale
# bearer even if the caller forgot to invalidate.
_cached_refresh_fingerprint: str | None = None

# Runtime status of the most recent exchange. Surfaced to the Settings UI
# so a silent mid-run failure (refresh token expired mid-batch, OpenAIRE
# down, etc.) doesn't leave the user wondering why rate limits tanked.
OpenaireExchangeStatus = Literal["never", "ok", "failed"]
_runtime_status: OpenaireExchangeStatus = "never"
_runtime_last_attempt_ms: int | None = None
_runtime_last_error: str | None = None


def _fingerprint_of(refresh_token: str) -> str:
    """Truncated fingerprint for cache-binding without storing a duplicate token.

    8-char prefix + length is enough to detect substitution; the raw token
    still lives on disk via settings, we're only using this for equality.
    """
    if not refresh_token:
        return ""
    return refresh_token[:8] + ":" + str(len(refresh_token))


def invalidate_cache() -> None:
    """Drop the cached access token.

    Called when the refresh token is replaced (user pasted a new one, or
    disconnected) so the next verifier request re-exchanges instead of
    reusing a token bound to a token the user just invalidated.
    """
    global _cached_access_token, _cached_expires_at, _cached_refresh_fingerprint
    _cached_access_token = None
    _cached_expires_at = 0.0
    _cached_refresh_fingerprint = None


def _refresh_window_cap(*, authenticated: bool) -> None:
    """Swap the OpenAIRE rate-limiter window cap to match auth state.

    Hour-window caps are 60 (anon) vs 7,200 (authed). Bump (or roll back)
    whenever the cached access-token validity flips. Imported lazily to
    keep this module free of a verifier import at the top — the token
    manager loads earlier than verifiers in some import orders.
    """
    try:
        from verifiers.openaire import update_window_for_auth_state
    except Exception:  # noqa: BLE001
        return
    try:
        update_window_for_auth_state(authenticated=authenticated)
    except Exception:  # noqa: BLE001, S110
        pass


def get_runtime_status() -> dict:
    """Return the most recent exchange outcome for the Settings UI."""
    return {
        "status": _runtime_status,
        "last_attempt_at_ms": _runtime_last_attempt_ms,
        "last_error": _runtime_last_error,
    }


async def exchange_refresh_token(
    refresh_token: str, *, timeout: float = 10.0
) -> dict:
    """Exchange a refresh token for a fresh access token.

    Returns the parsed JSON response. Raises :class:`RuntimeError` with a
    human-readable message on HTTP errors, timeouts, or malformed responses
    so the caller can forward the text to the UI.
    """
    params = {"refreshToken": refresh_token}
    try:
        ssl_ctx = create_ssl_context()
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=connector,
        ) as session, session.get(REFRESH_ENDPOINT, params=params) as resp:
            if resp.status == 401 or resp.status == 403:
                raise RuntimeError(
                    "OpenAIRE rejected the refresh token "
                    "(expired or mistyped)."
                )
            if resp.status != 200:
                body = (await resp.text())[:200]
                raise RuntimeError(
                    f"OpenAIRE returned HTTP {resp.status}: {body}"
                )
            data = await resp.json(content_type=None)
            if not isinstance(data, dict) or "access_token" not in data:
                raise RuntimeError(
                    "OpenAIRE response missing access_token field."
                )
            return data
    except asyncio.TimeoutError as exc:
        raise RuntimeError("Timed out contacting OpenAIRE.") from exc
    except aiohttp.ClientError as exc:
        raise RuntimeError(f"Network error contacting OpenAIRE: {exc}") from exc


async def _refresh_and_cache(refresh_token: str) -> str | None:
    """Call OpenAIRE, update the in-memory cache, and persist rotated tokens.

    Returns the new access token on success, ``None`` on any error (the
    caller falls back to anonymous). Errors are swallowed here — the verifier
    path should never raise just because auth glue hiccupped — but they are
    recorded in the runtime status so the Settings UI can surface them.
    """
    global _cached_access_token, _cached_expires_at, _cached_refresh_fingerprint
    global _runtime_status, _runtime_last_attempt_ms, _runtime_last_error
    _runtime_last_attempt_ms = int(time.time() * 1000)

    try:
        data = await exchange_refresh_token(refresh_token)
    except RuntimeError as exc:
        _runtime_status = "failed"
        _runtime_last_error = str(exc)
        # Exchange failed — verifier will fall back to anonymous requests,
        # so the rate-limiter window must roll back to the anon cap (60/hr)
        # or we'll cheerfully send the 61st request straight into a 429.
        _refresh_window_cap(authenticated=False)
        return None

    access_token = data.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        _runtime_status = "failed"
        _runtime_last_error = "OpenAIRE returned no access token."
        _refresh_window_cap(authenticated=False)
        return None

    # OpenAIRE advertises expires_in in seconds (usually 3600). Be defensive —
    # accept str or int, fall back to 55 min if the field is missing or
    # malformed, which still leaves us a full 5-minute safety buffer.
    raw_expires = data.get("expires_in", 3600)
    try:
        expires_in = float(raw_expires)
    except (TypeError, ValueError):
        expires_in = 3300.0

    _cached_access_token = access_token
    _cached_expires_at = (
        time.monotonic() + max(expires_in - _EXPIRY_SAFETY_MARGIN_SECONDS, 30.0)
    )
    _cached_refresh_fingerprint = _fingerprint_of(refresh_token)
    _runtime_status = "ok"
    _runtime_last_error = None
    # User has a working access token now — lift the OpenAIRE hour-window
    # cap from 60 (anon) to 7,200 (authed). Idempotent: re-calling on
    # successive refreshes is a no-op replacement of the same cap.
    _refresh_window_cap(authenticated=True)

    # OpenAIRE may rotate the refresh token. Persist the new one so the user
    # doesn't hit an unexpected sign-out next month. settings_store is a
    # neutral module so this is now a plain top-level import — the
    # circular-dependency dance against api.settings is gone.
    new_refresh = data.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh and new_refresh != refresh_token:
        try:
            current = get_current_settings()
            updated_keys = dict(current.api_keys)
            updated_keys["openaire"] = new_refresh
            updated = current.model_copy(update={
                "api_keys": updated_keys,
                "openaire_token_saved_at": datetime.now(timezone.utc)
                .date()
                .isoformat(),
            })
            save_settings(updated)
            # Re-bind the fingerprint so a racing verifier that reads the
            # freshly persisted refresh token right after this write doesn't
            # treat our just-cached access token as stale and re-exchange.
            _cached_refresh_fingerprint = _fingerprint_of(new_refresh)
        except Exception:  # noqa: BLE001, S110
            # Persistence failure is non-fatal: we still have a valid access
            # token in memory. The worst case is the user re-paste next month.
            pass

    return access_token


async def get_access_token() -> str | None:
    """Return a valid OpenAIRE access token, or ``None`` if unavailable.

    Resolves the current refresh token from app settings each call so a
    user's disconnect/reconnect during the session takes effect immediately.
    Also detects out-of-band token swaps via a fingerprint comparison, so a
    reconnect with a different token can't serve the prior bearer.
    """
    try:
        refresh_token = (
            get_current_settings().api_keys.get("openaire", "") or ""
        ).strip()
    except Exception:  # noqa: BLE001
        return None

    if not refresh_token:
        # User disconnected. Clear cached token so a later reconnect with a
        # different token can't round-trip the previous bearer.
        invalidate_cache()
        # Roll the hour-window cap back to anon (60) — the user is no
        # longer authenticated and must respect the stricter limit.
        _refresh_window_cap(authenticated=False)
        return None

    fingerprint = _fingerprint_of(refresh_token)

    async with _cache_lock:
        if (
            _cached_access_token
            and time.monotonic() < _cached_expires_at
            and _cached_refresh_fingerprint == fingerprint
        ):
            return _cached_access_token
        # Refresh token was replaced out-of-band (e.g. user edited the
        # settings file directly). Drop the stale access token before we
        # consider reusing it.
        if (
            _cached_refresh_fingerprint is not None
            and _cached_refresh_fingerprint != fingerprint
        ):
            invalidate_cache()
        return await _refresh_and_cache(refresh_token)
