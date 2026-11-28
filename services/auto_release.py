"""
services/auto_release.py — background worker that closes stale
`truck_assignments` rows when their truck has come back from a trip.

This is what makes auto-release actually work without requiring anyone
to be looking at the Live page. It runs a sweep every
`SWEEP_INTERVAL_SECONDS` (default 120 s).

Design
------
* One DB query lists every open assignment.
* For each truck, `fleet_state.current_state()` tells us where it is.
* If the state is `yard` / `waiting` / `maintenance` / `fueling` /
  `loading`, the assignment is released with reason `trip_ended`.
* If the state is `on_road`, `trip_time`, or `unknown`, the assignment
  stays open. `unknown` means we couldn't determine the state — it is
  not proof the truck is home, so we never release on it.
* A `None` state (lookup failure) also leaves the assignment open.

Public API
----------
  start()             — idempotent; call from app.py post-fork
  stop()              — graceful shutdown
  force_sweep_now()   — manual trigger, returns dict with counts
  last_sweep()        — dict with timestamp + counts (for UI display)
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS = 120
_SWEEP_JITTER_SECONDS  = 15

# Which fleet_state groups should trigger auto-release.
RELEASE_GROUPS = {
    "yard",
    "waiting",
    "maintenance",
    "fueling",
    "loading",
    "weighing",  
    "waybill", 
}

# Anything not in RELEASE_GROUPS is treated as "leave the assignment
# open". That explicitly includes on_road, trip_time, and unknown —
# unknown is not proof of anything and we never release on it.

_thread: threading.Thread | None = None
_stop_evt = threading.Event()
_last_sweep: dict = {
    "at": None,
    "open_assignments": 0,
    "released": 0,
    "errors": 0,
}

_sweep_lock = threading.Lock()


def _snapshot() -> dict:
    with _sweep_lock:
        return dict(_last_sweep)


def last_sweep() -> dict:
    return _snapshot()


# ---------------------------------------------------------------------------
# One sweep
# ---------------------------------------------------------------------------

def _list_open_assignments() -> list[dict]:
    from services.supabase_client import admin_client
    try:
        resp = (admin_client().table("truck_assignments")
                .select("id,driver_id,truck_id,imei,assigned_at")
                .is_("released_at", "null")
                .order("assigned_at", desc=False)
                .limit(2000)
                .execute())
        return resp.data or []
    except Exception:
        log.exception("auto_release: open assignments lookup failed")
        return []


def sweep_once() -> dict:
    """Run one release sweep. Safe to call from the worker or by hand."""
    from services import fleet_state
    from services import drivers as drivers_svc
    from jimi import RateLimitExceeded

    started_at = datetime.now(timezone.utc)
    open_rows  = _list_open_assignments()
    released   = 0
    errors     = 0
    rate_hit   = False

    for row in open_rows:
        imei = (row.get("imei") or "").strip()
        if not imei:
            errors += 1
            continue
        try:
            state = fleet_state.current_state(imei)
        except RateLimitExceeded:
            rate_hit = True
            break
        except Exception:
            log.exception("auto_release: state lookup failed for %s", imei)
            errors += 1
            continue

        # state is None means we couldn't determine the state (fetch
        # failed). Leave the assignment open — never release on a
        # lookup failure.
        if state is None:
            log.info(
                "auto_release: state lookup failed for %s (assignment %s); "
                "leaving assignment open", imei, row["id"],
            )
            errors += 1
            continue

        group = state.get("group")
        if group not in RELEASE_GROUPS:
            # on_road / trip_time / unknown — keep the assignment.
            continue
        try:
            drivers_svc.release_assignment(row["id"], reason="trip_ended")
            released += 1
            log.info(
                "auto_release: released assignment %s driver=%s truck=%s "
                "group=%s fence=%s",
                row["id"], row.get("driver_id"), row.get("truck_id"),
                group, state.get("fence"),
            )
        except Exception:
            log.exception("auto_release: release failed for %s", row["id"])
            errors += 1

    finished_at = datetime.now(timezone.utc)
    with _sweep_lock:
        _last_sweep.update({
            "at":               finished_at.isoformat(),
            "started_at":       started_at.isoformat(),
            "duration_s":       (finished_at - started_at).total_seconds(),
            "open_assignments": len(open_rows),
            "released":         released,
            "errors":           errors,
            "rate_limited":     rate_hit,
        })
    if open_rows:
        log.info(
            "auto_release sweep: open=%d released=%d errors=%d "
            "rate_limited=%s duration=%.1fs",
            len(open_rows), released, errors, rate_hit,
            (finished_at - started_at).total_seconds(),
        )
    return _snapshot()


# ---------------------------------------------------------------------------
# Worker lifecycle
# ---------------------------------------------------------------------------

def _loop():
    # small start jitter so we don't hammer Tracksolid right at boot
    _stop_evt.wait(5 + (_SWEEP_JITTER_SECONDS * 0.1))
    while not _stop_evt.is_set():
        try:
            sweep_once()
        except Exception:
            log.exception("auto_release: sweep crashed")
        # wait for interval (or shutdown)
        _stop_evt.wait(SWEEP_INTERVAL_SECONDS)


def start():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop_evt.clear()
    _thread = threading.Thread(
        target=_loop, name="auto-release", daemon=True,
    )
    _thread.start()
    log.info("auto_release worker started (every %ss)",
             SWEEP_INTERVAL_SECONDS)


def stop():
    _stop_evt.set()
    if _thread is not None:
        _thread.join(timeout=5)
    log.info("auto_release worker stopped")


def force_sweep_now() -> dict:
    """Synchronous trigger, used by the admin button."""
    return sweep_once()