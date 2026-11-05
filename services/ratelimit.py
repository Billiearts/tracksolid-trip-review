"""
services/ratelimit.py — Tracksolid API quota tracking.

Per the November 2026 Tracksolid update:

  * Daily quota: **86,400 calls/day per account** for all methods
    (per the support email; the FAQ table still shows 8,640 for
    "other nodes" — treat 86,400 as the operative number until
    support confirms otherwise).
  * `jimi.user.device.location.list` is also 86,400/day.
  * Windows are **rolling 24h from the first call**, not midnight UTC.
  * Concurrency cap: 100 calls/second (we peak at ~16).

This module tracks:

  1. `_state["window_started_at"]` — first successful call of the window.
  2. Per-method counters (`_state["calls_by_method"]`). Not persisted —
     they reset on process restart. See `/admin/usage`.
  3. A rate-limit flag (`limited_at`) that auto-clears 24h after the
     window started.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock

_lock = Lock()

# Quotas. Adjust both here if support confirms a different split.
QUOTA_TOTAL_PER_DAY    = 86400
QUOTA_LOCATION_PER_DAY = 86400
LOCATION_METHOD        = "jimi.device.location.get"   # note: .get, not .list

_state: dict = {
    "window_started_at": None,
    "limited_at":        None,
    "last_clear":        None,
    "message":           "",
    "calls_total":       0,
    "calls_by_method":   {},
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def note_call(method: str, ok: bool) -> None:
    """Record one Tracksolid API call."""
    with _lock:
        _state["calls_total"] += 1
        _state["calls_by_method"][method] = (
            _state["calls_by_method"].get(method, 0) + 1
        )
        if ok and _state["window_started_at"] is None:
            _state["window_started_at"] = _now()


def note_rate_limited(message: str = "") -> None:
    """Record that a 1006 was just received."""
    with _lock:
        if _state["limited_at"] is None:
            _state["limited_at"] = _now()
        if message:
            _state["message"] = message


def note_call_success() -> None:
    """Backward-compat alias. Use note_call(method, ok=True) instead."""
    note_call("(unknown)", ok=True)


def clear() -> None:
    """Clear the rate-limit flag (does NOT reset counters)."""
    with _lock:
        if _state["limited_at"] is not None:
            _state["last_clear"] = _now()
        _state["limited_at"] = None
        _state["message"] = ""


def reset() -> None:
    """Full reset — flag, window, counters. Admin-only."""
    with _lock:
        _state["window_started_at"] = None
        _state["limited_at"] = None
        _state["last_clear"] = _now()
        _state["message"] = ""
        _state["calls_total"] = 0
        _state["calls_by_method"] = {}


def is_rate_limited() -> bool:
    with _lock:
        return _state["limited_at"] is not None


def status() -> dict:
    with _lock:
        now = _now()

        anchor = _state["window_started_at"] or _state["limited_at"]
        reset_at = (anchor + timedelta(hours=24)) if anchor else None

        if reset_at is not None and now >= reset_at:
            _state["window_started_at"] = None
            _state["limited_at"] = None
            _state["message"] = ""
            _state["last_clear"] = now
            anchor = None
            reset_at = None

        calls_total = _state["calls_total"]
        by_method = dict(_state["calls_by_method"])
        location_calls = by_method.get(LOCATION_METHOD, 0)

        percent_total = (calls_total / QUOTA_TOTAL_PER_DAY * 100.0
                         if QUOTA_TOTAL_PER_DAY else 0.0)
        percent_location = (location_calls / QUOTA_LOCATION_PER_DAY * 100.0
                            if QUOTA_LOCATION_PER_DAY else 0.0)

        return {
            "rate_limited":      _state["limited_at"] is not None,
            "limited_at":        _iso(_state["limited_at"]),
            "window_started_at": _iso(anchor),
            "resets_at":         _iso(reset_at),
            "last_clear":        _iso(_state["last_clear"]),
            "message":           _state["message"],
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