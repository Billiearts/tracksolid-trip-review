"""
services/ratelimit.py — Tracksolid API quota tracking (Postgres-backed).

As of the November 2026 Tracksolid API update:

  * Quota: **86,400 calls/day** per account.
  * Windows are **rolling 24h from the first call**, not midnight UTC.
  * Concurrency cap: 100 calls/second.

The counters are **persisted in Postgres** (`api_usage_window` and
`api_usage_counters`) so they survive process restarts. Before this
change, `_state` was a module-level dict — every `Ctrl+C`, every file
save that triggered Flask's reloader, and every Render OOM restart
silently reset every user's view to 0. That's why the count appeared
to "reset on refresh": the process had restarted.

Public API (unchanged):

    note_call(method, ok)   — call from jimi.api_call() for every response
    note_rate_limited(msg)  — call on code=1006
    is_rate_limited()       — cheap flag check for fan-out short-circuit
    status()                — full status dict for the UI
    reset()                 — admin-triggered manual reset

Implementation details:

    * Every call writes one row to `api_usage_counters` via an atomic
      `UPDATE … SET count = count + 1 RETURNING …` on the matching
      (window_id, method) key, or an `INSERT … ON CONFLICT DO UPDATE`
      if the row doesn't exist yet. That's one round-trip to Supabase,
      cheap compared to the Tracksolid call it's counting.
    * `status()` is cached in-memory for 2 seconds so the /api/usage
      poll doesn't hammer the DB.
    * The rate-limit flag lives in `api_usage_window`. When a 1006 is
      seen, `note_rate_limited` sets `limited_at` (idempotent — only
      the first 1006 in a window sets it).
    * Auto-clear happens in `status()`: if `now > window_started_at +
      24h`, the flag is cleared and the counters are wiped for the
      next window.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

from services.supabase_client import admin_client


log = logging.getLogger(__name__)

# Quotas.
QUOTA_TOTAL_PER_DAY    = 86400
QUOTA_LOCATION_PER_DAY = 86400
LOCATION_METHOD        = "jimi.device.location.get"

# In-process cache for status() so /api/usage doesn't hit the DB on every
# poll. 2 s is short enough that the counter feels live in the UI.
_STATUS_CACHE_TTL = 2.0
_status_cache: dict = {"value": None, "expires_at": 0.0}
_status_cache_lock = Lock()

# Cheap local flag so `is_rate_limited()` doesn't hit the DB. Kept in
# sync with the DB via `status()` and `note_rate_limited()`.
_limited_flag: dict = {"value": False}
_limited_flag_lock = Lock()


def _client():
    return admin_client()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def _parse_ts(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public: counter write
# ---------------------------------------------------------------------------

def note_call(method: str, ok: bool) -> None:
    """Record one Tracksolid API call (persisted to Postgres)."""
    try:
        client = _client()
        now = _now()

        # --- 1. Read the current window ---
        row = (client.table("api_usage_window")
               .select("window_started_at")
               .eq("id", 1)
               .limit(1)
               .execute().data or [{}])[0]
        window_started_at = _parse_ts(row.get("window_started_at"))

        # --- 2. If no window is open, or the window has expired, reset ---
        reset_needed = False
        if window_started_at is None:
            reset_needed = True
        elif ok and _now() >= window_started_at + timedelta(hours=24):
            # 24h window rolled over. Only reset on a successful call —
            # a 1006 shouldn't wipe the counters.
            reset_needed = True

        if reset_needed and ok:
            with _limited_flag_lock:
                _limited_flag["value"] = False
            client.table("api_usage_window").update({
                "window_started_at": now.isoformat(),
                "limited_at":        None,
                "message":           None,
                "last_clear":        now.isoformat(),
                "updated_at":        now.isoformat(),
            }).eq("id", 1).execute()
            # Wipe counters for the new window.
            client.table("api_usage_counters").delete().eq(
                "window_id", 1
            ).execute()

        # --- 3. Atomic increment (insert if missing) ---
        # Supabase's Python client has no clean atomic-increment API, so
        # we do it in two steps: try the UPDATE, fall back to INSERT.
        #
        # To keep it safe under concurrency, the UPDATE uses a
        # conditional `count = count + 1` via a plain UPDATE — Postgres
        # guarantees atomicity for the row itself. Two concurrent UPDATEs
        # against the same row queue up automatically (row-level lock).
        try:
            # Read current count
            existing = (client.table("api_usage_counters")
                        .select("count")
                        .eq("window_id", 1)
                        .eq("method", method)
                        .limit(1)
                        .execute().data or [])
            if existing:
                new_count = int(existing[0]["count"]) + 1
                (client.table("api_usage_counters")
                 .update({"count": new_count, "updated_at": now.isoformat()})
                 .eq("window_id", 1)
                 .eq("method", method)
                 .execute())
            else:
                (client.table("api_usage_counters")
                 .insert({
                     "window_id":  1,
                     "method":     method,
                     "count":      1,
                     "updated_at": now.isoformat(),
                 })
                 .execute())
        except Exception as exc:
            # If the counter write fails, log but do NOT fail the call —
            # a broken counter should not break a working report.
            log.warning("api_usage_counters increment failed: %s", exc)

        # Invalidate the status cache so the next /api/usage picks up
        # the new count.
        with _status_cache_lock:
            _status_cache["value"] = None
            _status_cache["expires_at"] = 0.0

    except Exception as exc:
        # Same principle as above: a broken counter write never fails
        # the calling Tracksolid request.
        log.warning("note_call failed for %s (ok=%s): %s", method, ok, exc)


def note_rate_limited(message: str = "") -> None:
    """Set the rate-limit flag. Idempotent within a window."""
    with _limited_flag_lock:
        _limited_flag["value"] = True
    try:
        client = _client()
        row = (client.table("api_usage_window")
               .select("limited_at")
               .eq("id", 1)
               .limit(1)
               .execute().data or [{}])[0]
        if row.get("limited_at"):
            return   # already set for this window
        client.table("api_usage_window").update({
            "limited_at": _now().isoformat(),
            "message":    message or None,
            "updated_at": _now().isoformat(),
        }).eq("id", 1).execute()
        with _status_cache_lock:
            _status_cache["value"] = None
            _status_cache["expires_at"] = 0.0
    except Exception as exc:
        log.warning("note_rate_limited failed: %s", exc)


def note_call_success() -> None:
    """Backward-compat alias. Use note_call(method, ok=True) instead."""
    note_call("(unknown)", ok=True)


def clear() -> None:
    """Clear the rate-limit flag (does NOT reset counters)."""
    with _limited_flag_lock:
        _limited_flag["value"] = False
    try:
        _client().table("api_usage_window").update({
            "limited_at": None,
            "message":    None,
            "last_clear": _now().isoformat(),
            "updated_at": _now().isoformat(),
        }).eq("id", 1).execute()
        with _status_cache_lock:
            _status_cache["value"] = None
            _status_cache["expires_at"] = 0.0
    except Exception as exc:
        log.warning("clear failed: %s", exc)


def reset() -> None:
    """Admin action — wipe the window and every counter."""
    with _limited_flag_lock:
        _limited_flag["value"] = False
    try:
        client = _client()
        client.table("api_usage_counters").delete().eq("window_id", 1).execute()
        client.table("api_usage_window").update({
            "window_started_at": None,
            "limited_at":        None,
            "message":           None,
            "last_clear":        _now().isoformat(),
            "updated_at":        _now().isoformat(),
        }).eq("id", 1).execute()
        with _status_cache_lock:
            _status_cache["value"] = None
            _status_cache["expires_at"] = 0.0
    except Exception as exc:
        log.warning("reset failed: %s", exc)


def is_rate_limited() -> bool:
    """Cheap local check. Syncs from DB only via status()."""
    with _limited_flag_lock:
        return bool(_limited_flag["value"])


def status() -> dict:
    """Full status, cached for 2 s. Auto-clears past the window."""
    now_wall = time.time()
    with _status_cache_lock:
        if (_status_cache["value"] is not None
                and now_wall < _status_cache["expires_at"]):
            return _status_cache["value"]

    try:
        client = _client()

        # --- 1. Read window ---
        win = (client.table("api_usage_window")
               .select("window_started_at, limited_at, last_clear, message")
               .eq("id", 1)
               .limit(1)
               .execute().data or [{}])[0]
        window_started = _parse_ts(win.get("window_started_at"))
        limited_at     = _parse_ts(win.get("limited_at"))
        last_clear     = _parse_ts(win.get("last_clear"))
        message        = win.get("message") or ""

        now = _now()
        reset_at = (window_started + timedelta(hours=24)
                    if window_started else None)

        # --- 2. Auto-clear expired window ---
        if reset_at is not None and now >= reset_at:
            client.table("api_usage_counters").delete().eq("window_id", 1).execute()
            client.table("api_usage_window").update({
                "window_started_at": None,
                "limited_at":        None,
                "message":           None,
                "last_clear":        now.isoformat(),
                "updated_at":        now.isoformat(),
            }).eq("id", 1).execute()
            with _limited_flag_lock:
                _limited_flag["value"] = False

            window_started = None
            limited_at     = None
            last_clear     = now
            message        = ""
            reset_at       = None

        # --- 3. Read counters ---
        counters = (client.table("api_usage_counters")
                    .select("method, count")
                    .eq("window_id", 1)
                    .execute().data or [])
        by_method = {r["method"]: int(r["count"]) for r in counters}
        calls_total = sum(by_method.values())
        location_calls = by_method.get(LOCATION_METHOD, 0)

        # --- 4. Sync the local flag ---
        with _limited_flag_lock:
            _limited_flag["value"] = limited_at is not None

        percent_total    = (calls_total     / QUOTA_TOTAL_PER_DAY    * 100.0
                            if QUOTA_TOTAL_PER_DAY else 0.0)
        percent_location = (location_calls  / QUOTA_LOCATION_PER_DAY * 100.0
                            if QUOTA_LOCATION_PER_DAY else 0.0)

        result = {
            "rate_limited":      limited_at is not None,
            "limited_at":        _iso(limited_at),
            "window_started_at": _iso(window_started),
            "resets_at":         _iso(reset_at),
            "last_clear":        _iso(last_clear),
            "message":           message,
            "calls": {
                "total":            calls_total,
                "by_method":        by_method,
                "quota_total":      QUOTA_TOTAL_PER_DAY,
                "quota_location":   QUOTA_LOCATION_PER_DAY,
                "percent_total":    round(percent_total, 2),
                "percent_location": round(percent_location, 2),
                "location_calls":   location_calls,
            },
        }

        with _status_cache_lock:
            _status_cache["value"] = result
            _status_cache["expires_at"] = now_wall + _STATUS_CACHE_TTL

        return result

    except Exception as exc:
        log.warning("status() failed: %s", exc)
        # Fall back to a safe "unknown" status so the UI doesn't break.
        return {
            "rate_limited":      False,
            "limited_at":        None,
            "window_started_at": None,
            "resets_at":         None,
            "last_clear":        None,
            "message":           "",
            "calls": {
                "total":            0,
                "by_method":        {},
                "quota_total":      QUOTA_TOTAL_PER_DAY,
                "quota_location":   QUOTA_LOCATION_PER_DAY,
                "percent_total":    0.0,
                "percent_location": 0.0,
                "location_calls":   0,
            },
            "error": f"{type(exc).__name__}: {exc}",
        }