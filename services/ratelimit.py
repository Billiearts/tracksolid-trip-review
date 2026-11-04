"""
services/ratelimit.py — tracks the Tracksolid daily quota state.

State is a single module-level flag. It clears itself at the next UTC
midnight after the limit was first noted, since that's when Tracksolid
resets its daily counter.

`last_clear` is set when the flag transitions from True to False so the
frontend can detect "just cleared" and drop the banner immediately
instead of waiting for the next 10-second poll.

`is_rate_limited()` is a cheap, non-mutating read used by the parallel
fan-out in app.py to short-circuit work as soon as the first 1006 lands.
It deliberately does NOT run the auto-clear logic — that happens in
`status()`, which is what the front-end polls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock

_lock = Lock()
_state: dict = {
    "limited_at": None,
    "message": "",
    "last_clear": None,
}


def note_rate_limited(message: str = "") -> None:
    """Record that a 1006 response was just received."""
    with _lock:
        if _state["limited_at"] is None:
            _state["limited_at"] = datetime.now(timezone.utc)
        if message:
            _state["message"] = message


def clear() -> None:
    """Manually clear (used when a successful call proves quota is back)."""
    with _lock:
        if _state["limited_at"] is not None:
            _state["last_clear"] = datetime.now(timezone.utc)
        _state["limited_at"] = None
        _state["message"] = ""


def is_rate_limited() -> bool:
    """Cheap, non-mutating read: is the daily quota currently exhausted?

    Used by parallel fan-out loops (`/trips`, `/preview`) to bail out the
    moment a 1006 lands, instead of letting every remaining future fire
    and time out. Does NOT auto-clear at midnight — that side effect
    belongs to `status()`, which the front-end polls.
    """
    with _lock:
        return _state["limited_at"] is not None


def status() -> dict:
    """Current rate-limit status. Auto-clears past UTC midnight."""
    with _lock:
        limited_at = _state["limited_at"]
        if limited_at is None:
            return {
                "rate_limited": False,
                "last_clear": (
                    _state["last_clear"].strftime("%Y-%m-%d %H:%M:%S")
                    if _state["last_clear"] else None
                ),
            }

        now = datetime.now(timezone.utc)
        reset_at = (limited_at + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if now >= reset_at:
            _state["last_clear"] = now
            _state["limited_at"] = None
            _state["message"] = ""
            return {
                "rate_limited": False,
                "last_clear": now.strftime("%Y-%m-%d %H:%M:%S"),
            }

        return {
            "rate_limited": True,
            "limited_at":   limited_at.strftime("%Y-%m-%d %H:%M:%S"),
            "resets_at":    reset_at.strftime("%Y-%m-%d %H:%M:%S"),
            "message":      _state["message"],
        }