"""
blueprints/trip_history.py — Completed trips (standalone blueprint).

Routes
------
  GET /trip-history/    single page: date range + free-text search

Quick ranges: today, yesterday, 7d, 30d, this_week, this_month,
last_month, this_year, last_year. Explicit start/end override the
quick range when both are supplied.

Search matches (case-insensitive substring) against truck name, IMEI,
driver name, and driver ID. Results are sorted descending by trip end
so the most recent lands on top.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date

from flask import (
    Blueprint, render_template, request, session, abort,
)

from decorators import login_required
from services import trip_history as th_svc
from jimi import RateLimitExceeded


log = logging.getLogger(__name__)
bp = Blueprint("trip_history", __name__, url_prefix="/trip-history")


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def _can_view() -> bool:
    if session.get("role") in ("admin", "super_admin"):
        return True
    return "trip_history_view" in (session.get("extra_roles") or [])


def _is_admin() -> bool:
    return session.get("role") in ("admin", "super_admin")


def _require_view():
    if not _can_view():
        abort(403)


# ---------------------------------------------------------------------------
# Range helpers
# ---------------------------------------------------------------------------

def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def _date_to_dt(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _quick_range_bounds(key: str, today: date | None = None
                        ) -> tuple[datetime, datetime] | None:
    if not key:
        return None
    today = today or _utc_today()
    if key == "today":
        s = today
        e = today + timedelta(days=1)
    elif key == "yesterday":
        s = today - timedelta(days=1)
        e = today
    elif key == "7d":
        s = today - timedelta(days=7)
        e = today + timedelta(days=1)
    elif key == "30d":
        s = today - timedelta(days=30)
        e = today + timedelta(days=1)
    elif key == "this_week":
        s = today - timedelta(days=today.weekday())
        e = s + timedelta(days=7)
    elif key == "this_month":
        s = today.replace(day=1)
        if s.month == 12:
            e = s.replace(year=s.year + 1, month=1)
        else:
            e = s.replace(month=s.month + 1)
    elif key == "last_month":
        this_start = today.replace(day=1)
        prev_end   = this_start - timedelta(days=1)
        s = prev_end.replace(day=1)
        e = this_start
    elif key == "this_year":
        s = today.replace(month=1, day=1)
        e = s.replace(year=s.year + 1)
    elif key == "last_year":
        s = today.replace(month=1, day=1, year=today.year - 1)
        e = today.replace(month=1, day=1)
    else:
        return None
    return _date_to_dt(s), _date_to_dt(e)


def _resolve_range(args) -> tuple[datetime, datetime, str, str, str]:
    """Return (begin_dt, end_dt, start_iso, end_iso, quick_key).

    start_iso / end_iso are the strings the date pickers should show.
    end_dt is EXCLUSIVE (midnight of the day after end_iso).
    """
    start_s = (args.get("start") or "").strip()
    end_s   = (args.get("end")   or "").strip()
    rng     = (args.get("range") or "").strip()

    start_d = _parse_date(start_s)
    end_d   = _parse_date(end_s)

    if start_d and end_d:
        if end_d < start_d:
            start_d, end_d = end_d, start_d
        begin = _date_to_dt(start_d)
        end   = _date_to_dt(end_d) + timedelta(days=1)   # inclusive end day
        return begin, end, start_d.isoformat(), end_d.isoformat(), ""

    bounds = _quick_range_bounds(rng) if rng else None
    if bounds is not None:
        begin, end = bounds
        return (begin, end,
                begin.date().isoformat(),
                (end - timedelta(days=1)).date().isoformat(),
                rng)

    begin, end = _quick_range_bounds("today")
    return (begin, end,
            begin.date().isoformat(),
            (end - timedelta(days=1)).date().isoformat(),
            "today")


# ---------------------------------------------------------------------------
# Single route
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    _require_view()

    begin, end, start_s, end_s, rng = _resolve_range(request.args)
    q = (request.args.get("q") or "").strip()

    trips, warning = [], ""
    try:
        trips = th_svc.trips_by_range(begin, end)
    except RateLimitExceeded:
        warning = ("Tracksolid daily API limit reached. "
                   "Try again later or reset the quota from Admin → Usage.")
    except Exception as exc:
        log.exception("trip_history lookup failed")
        warning = f"Could not fetch trips from Tracksolid: {exc}"

    # Apply text search *after* the Tracksolid fetch. The fetch is
    # bounded by the date range, so this is O(trips) not O(fleet).
    if q:
        needle = q.lower()
        trips = [
            t for t in trips
            if needle in (t.get("truck_id") or "").lower()
            or needle in (t.get("imei") or "").lower()
            or needle in (t.get("driver_name") or "").lower()
            or needle in (t.get("driver_id") or "").lower()
        ]

    # Sort descending by trip end so the most recent lands on top.
    trips.sort(key=lambda t: t["end"], reverse=True)

    total = len(trips)
    unique_drivers = len({t["driver_id"] for t in trips if t["driver_id"]})
    unique_trucks  = len({t["truck_id"]  for t in trips if t["truck_id"]})

    return render_template(
        "trip_history/index.html",
        start=start_s, end=end_s, range_key=rng,
        search=q,
        trips=trips,
        total=total,
        unique_drivers=unique_drivers,
        unique_trucks=unique_trucks,
        warning=warning,
        is_admin=_is_admin(),
    )