"""
services/ratelimit.py — tracks the Tracksolid daily quota state.

State is a single module-level flag. It clears itself at the next UTC
midnight after the limit was first noted, since that's when Tracksolid
resets its daily counter.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock

_lock = Lock()
_state: dict = {"limited_at": None, "message": ""}


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
        _state["limited_at"] = None
        _state["message"] = ""


def status() -> dict:
    """Current rate-limit status. Auto-clears past UTC midnight."""
    with _lock:
        limited_at = _state["limited_at"]
        if limited_at is None:
            return {"rate_limited": False}

        now = datetime.now(timezone.utc)
        reset_at = (limited_at + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        if now >= reset_at:
            _state["limited_at"] = None
            _state["message"] = ""
            return {"rate_limited": False}

        return {
            "rate_limited": True,
            "limited_at":   limited_at.strftime("%Y-%m-%d %H:%M:%S"),
            "resets_at":    reset_at.strftime("%Y-%m-%d %H:%M:%S"),
            "message":      _state["message"],
        }