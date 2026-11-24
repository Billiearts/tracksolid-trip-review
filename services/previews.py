"""
services/previews.py — in-memory store for computed previews.

State lives for the life of the process. Oldest previews are evicted once
_PREVIEW_LIMIT is exceeded.

A preview state dict now carries driver attribution when it was created
from a driver search:
    driver_id   — str | None
    driver_name — str
These are read back by templates that show the driver header, and by
app.py when it constructs the eventual reports row.
"""
from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock


_PREVIEW_LIMIT = 200
_previews: dict[str, dict] = {}
_previews_lock = Lock()


def store_preview(preview_id: str, state: dict) -> None:
    """Insert or replace a preview; evict oldest if over the limit."""
    with _previews_lock:
        _previews[preview_id] = state
        if len(_previews) > _PREVIEW_LIMIT:
            oldest = sorted(
                _previews.items(),
                key=lambda kv: kv[1].get("queued_at")
                or datetime.min.replace(tzinfo=timezone.utc),
            )
            for k, _ in oldest[:len(_previews) - _PREVIEW_LIMIT]:
                _previews.pop(k, None)


def get_preview(preview_id: str) -> dict | None:
    """Return the state dict for a preview, or None if missing/evicted."""
    with _previews_lock:
        return _previews.get(preview_id)


def mark_ready(preview_id: str, data: dict) -> None:
    with _previews_lock:
        state = _previews.get(preview_id)
        if state is None:
            return
        state["status"] = "ready"
        state["data"] = data
        state["finished_at"] = datetime.now(timezone.utc)


def mark_failed(preview_id: str, error: str) -> None:
    with _previews_lock:
        state = _previews.get(preview_id)
        if state is None:
            return
        state["status"] = "failed"
        state["error"] = error
        state["finished_at"] = datetime.now(timezone.utc)


def mark_ready_reports(preview_id: str, reports: list) -> None:
    """Mark a multi-trip preview as successfully computed."""
    with _previews_lock:
        state = _previews.get(preview_id)
        if state is None:
            return
        state["status"] = "ready"
        state["reports"] = reports
        state["finished_at"] = datetime.now(timezone.utc)