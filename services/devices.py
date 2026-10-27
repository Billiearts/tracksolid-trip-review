"""
services/devices.py — shared device list + lookup.

One fetch of jimi.user.device.list is cached for 5 minutes and reused by
both the Flask app and the CLI.

Pagination: the first call mirrors the original minimal request (target
only). If the response advertises a total larger than the batch, we page.
If the API rejects page/page_size, we log and accept the single page.

Truck classification (regular / operational / ambiguous) is used by the
live fleet feature to hide non-corridor trucks from the map.
"""
from __future__ import annotations

import logging
import re
import time

from jimi import api_call, USER_ID


log = logging.getLogger(__name__)


_devices_cache: dict = {"data": None, "expires_at": 0.0}
_DEVICES_TTL_SECONDS = 300
_PAGE_SIZE = 500
_MAX_PAGES = 50


# Tier 1 — strict "regular fleet truck" name pattern.
_REGULAR_TRUCK_RE = re.compile(
    r"^\s*(?:GT|GN|GR|GE|GC)\s*\d{3,5}\s*-\s*\d{2}\s*$",
    re.IGNORECASE,
)

# Tier 2 — known operational-fleet keywords (case-insensitive substring).
_OPERATIONAL_KEYWORDS = (
    "canteen",
    "service van",
    "bowser",
    "crane",
    "tow truck",
    "tdi",
    "hlge",
    "bogoso",
    "mining",
)


def _extract_batch_and_total(response: dict) -> tuple[list[dict], int | None]:
    """Return (batch, total) from whatever shape the API sends."""
    result = response.get("result")
    if result is None:
        result = response.get("data")

    if isinstance(result, list):
        return result, None

    if isinstance(result, dict):
        batch = result.get("list")
        if batch is None:
            batch = result.get("items")
        if batch is None:
            batch = result.get("data")
        if batch is None:
            batch = []
        total = result.get("total")
        if total is None:
            total = result.get("count")
        try:
            total = int(total) if total is not None else None
        except (TypeError, ValueError):
            total = None
        return list(batch), total

    return [], None


def _fetch_page(page: int) -> tuple[list[dict], int | None]:
    if page == 1:
        response = api_call("jimi.user.device.list", {"target": USER_ID})
        return _extract_batch_and_total(response)

    try:
        response = api_call("jimi.user.device.list", {
            "target":    USER_ID,
            "page":      page,
            "page_size": _PAGE_SIZE,
        })
    except Exception as exc:
        log.warning("paged call rejected (%s); accepting first page only", exc)
        return [], None
    return _extract_batch_and_total(response)


def _fetch_all_devices() -> list[dict]:
    all_devices: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        batch, total = _fetch_page(page)
        if not batch:
            break
        all_devices.extend(batch)

        if total is None:
            break
        if len(all_devices) >= total:
            break
        if len(batch) < _PAGE_SIZE:
            break
        page += 1

    log.info("device list fetched: %d device(s) across %d page(s)",
             len(all_devices), page)
    return all_devices


def get_devices() -> list[dict]:
    """Return the raw device list, cached for 5 minutes."""
    now = time.time()
    if _devices_cache["data"] is not None and now < _devices_cache["expires_at"]:
        return _devices_cache["data"]

    devices = _fetch_all_devices()
    _devices_cache["data"] = devices
    _devices_cache["expires_at"] = now + _DEVICES_TTL_SECONDS
    return devices


def classify_device(device_name: str) -> str:
    """Return 'regular', 'operational', or 'ambiguous' for a truck name."""
    name = (device_name or "").strip()
    if not name:
        return "operational"
    lower = name.lower()
    for kw in _OPERATIONAL_KEYWORDS:
        if kw in lower:
            return "operational"
    if _REGULAR_TRUCK_RE.match(name):
        return "regular"
    return "ambiguous"


def find_device(query: str) -> dict | None:
    """Resolve a query to a single device."""
    devices = get_devices()
    q = query.strip().lower()
    if not q:
        return None

    if q.isdigit() and len(q) >= 10:
        for d in devices:
            if d.get("imei") == query.strip():
                return d
        return None

    matches = [d for d in devices if q in (d.get("deviceName") or "").lower()]
    return matches[0] if len(matches) == 1 else None


def resolve_device(query: str) -> tuple[dict | None, list[dict]]:
    devices = get_devices()
    q = query.strip().lower()
    if not q:
        return None, []

    if q.isdigit() and len(q) >= 10:
        for d in devices:
            if d.get("imei") == query.strip():
                return d, [d]
        return None, []

    matches = [d for d in devices if q in (d.get("deviceName") or "").lower()]
    if len(matches) == 1:
        return matches[0], matches
    return None, matches