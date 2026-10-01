"""
services/sessions.py — server-side session records.

Cookies carry only a session id. On every request, the id is validated
against Postgres — but results are cached for 30 seconds, so most requests
serve from memory without a network call.

Revocation is still immediate-ish: a deleted session is gone within at most
30 seconds on the affected client. If that's too long, drop
_SESSION_CACHE_TTL to 5 or 10 seconds.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


DEFAULT_TTL = timedelta(days=14)

# --- caches ---------------------------------------------------------------

_session_cache: dict[str, tuple[float, dict]] = {}
_session_cache_lock = Lock()
_SESSION_CACHE_TTL = 30   # seconds

_last_touch: dict[str, float] = {}
_TOUCH_INTERVAL = 60      # seconds between last_seen_at writes


# --- writes ---------------------------------------------------------------

def create(*, user_id: str, email: str, role: str,
           full_name: str | None) -> str:
    now = datetime.now(timezone.utc)
    expires_at = now + DEFAULT_TTL
    client = admin_client()
    resp = client.table("user_sessions").insert({
        "user_id":    user_id,
        "email":      email,
        "role":       role,
        "full_name":  full_name,
        "expires_at": expires_at.isoformat(),
    }).execute()

    rows = resp.data or []
    if not rows:
        raise RuntimeError("session create returned no row")
    return rows[0]["id"]


def delete(session_id: str) -> None:
    if not session_id:
        return
    with _session_cache_lock:
        _session_cache.pop(session_id, None)
        _last_touch.pop(session_id, None)
    client = admin_client()
    try:
        client.table("user_sessions").delete().eq("id", session_id).execute()
    except Exception as exc:
        log.debug("session delete failed: %s", exc)


def delete_for_user(user_id: str) -> int:
    with _session_cache_lock:
        to_drop = [
            sid for sid, (_, rec) in _session_cache.items()
            if rec.get("user_id") == user_id
        ]
        for sid in to_drop:
            _session_cache.pop(sid, None)
            _last_touch.pop(sid, None)

    client = admin_client()
    try:
        resp = client.table("user_sessions").delete().eq("user_id", user_id).execute()
        return len(resp.data or [])
    except Exception as exc:
        log.warning("session delete_for_user failed: %s", exc)
        return 0


def purge_expired() -> int:
    client = admin_client()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        resp = (
            client.table("user_sessions")
            .delete()
            .lt("expires_at", now_iso)
            .execute()
        )
        return len(resp.data or [])
    except Exception as exc:
        log.warning("session purge failed: %s", exc)
        return 0


# --- reads ----------------------------------------------------------------

def load(session_id: str) -> dict | None:
    """Validate and return a session. Cached for _SESSION_CACHE_TTL seconds."""
    if not session_id:
        return None

    now = time.time()

    # Cache hit?
    with _session_cache_lock:
        entry = _session_cache.get(session_id)
        if entry is not None:
            expires_at, record = entry
            if now < expires_at:
                return record
            _session_cache.pop(session_id, None)

    # Cache miss — hit Supabase
    client = admin_client()
    try:
        resp = (
            client.table("user_sessions")
            .select("*")
            .eq("id", session_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        log.warning("session load failed: %s", exc)
        return None

    rows = resp.data or []
    if not rows:
        return None

    row = rows[0]

    try:
        expires_at = datetime.fromisoformat(
            str(row["expires_at"]).replace("Z", "+00:00")
        )
    except (ValueError, KeyError):
        delete(session_id)
        return None

    if expires_at <= datetime.now(timezone.utc):
        delete(session_id)
        return None

    record = {
        "id":        row["id"],
        "user_id":   row["user_id"],
        "email":     row.get("email"),
        "role":      row.get("role") or "user",
        "full_name": row.get("full_name"),
    }

    with _session_cache_lock:
        _session_cache[session_id] = (now + _SESSION_CACHE_TTL, record)

    # Throttled last_seen_at update
    if now - _last_touch.get(session_id, 0) >= _TOUCH_INTERVAL:
        _last_touch[session_id] = now
        try:
            client.table("user_sessions").update({
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", session_id).execute()
        except Exception as exc:
            log.debug("session touch failed: %s", exc)

    return record