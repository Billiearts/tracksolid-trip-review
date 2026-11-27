"""
blueprints/trip_history.py — Completed trips (standalone blueprint).

Routes
------
  GET /trip-history/                    day view (default: today)
  GET /trip-history/by-driver           range + multi-driver
  GET /trip-history/by-truck            range + multi-truck

Quick ranges: today, yesterday, 7d, 30d, this_week, this_month,
last_month, this_year, last_year. Explicit start/end override the
quick range when both are supplied.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, date

from flask import (
    Blueprint, render_template, request, session, abort,
)

from decorators import login_required
from services import drivers as drivers_svc
from services import devices as devices_svc
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
# Combobox data
# ---------------------------------------------------------------------------

def _all_drivers_for_picker() -> list[dict]:
    try:
        res = drivers_svc.list_drivers(
            page=1, per_page=1000,
            include_inactive=False, include_deleted=False)
        out = [
            {"driver_id": d["id"],
             "full_name": d["full_name"],
             "staff_id":  d.get("staff_id") or ""}
            for d in res["drivers"]
        ]
        out.sort(key=lambda x: x["full_name"].lower())
        return out
    except Exception:
        log.exception("trip_history: driver list failed")
        return []


def _all_trucks_for_picker() -> list[dict]:
    try:
        devs = None
        try:
            devs = devices_svc.get_devices_cached_only()
        except Exception:
            devs = None
        if devs is None:
            devs = devices_svc.get_devices()
    except Exception:
        log.exception("trip_history: device list failed")
        return []
    seen, out = set(), []
    for d in devs or []:
        name = (d.get("deviceName") or "").strip()
        imei = (d.get("imei") or "").strip()
        if not name or not imei or imei in seen:
            continue
        seen.add(imei)
        try:
            cat = devices_svc.classify_device(name)
        except Exception:
            cat = "regular"
        if cat != "regular":
            continue
        out.append({"imei": imei, "truck_id": name, "name": name})
    out.sort(key=lambda x: x["name"].lower())
    return out


# ---------------------------------------------------------------------------
# Day view
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    _require_view()

    begin, end, start_s, end_s, _rng = _resolve_range(request.args)

    trips, warning = [], ""
    try:
        trips = th_svc.trips_by_range(begin, end)
    except RateLimitExceeded:
        warning = ("Tracksolid daily API limit reached. "
                   "Try again later or reset the quota from Admin → Usage.")
    except Exception as exc:
        log.exception("trip_history day view failed")
        warning = f"Could not fetch trips from Tracksolid: {exc}"

    drivers = {t["driver_id"] for t in trips if t["driver_id"]}
    trucks  = {t["truck_id"] for t in trips if t["truck_id"]}

    return render_template(
        "trip_history/index.html",
        start=start_s, end=end_s,
        trips=trips,
        total=len(trips),
        unique_drivers=len(drivers),
        unique_trucks=len(trucks),
        warning=warning,
        is_admin=_is_admin(),
    )


# ---------------------------------------------------------------------------
# By driver
# ---------------------------------------------------------------------------

@bp.route("/by-driver")
@login_required
def by_driver():
    _require_view()

    begin, end, start_s, end_s, _rng = _resolve_range(request.args)

    ids_raw = request.args.getlist("driver_id")
    driver_ids = [d for d in ids_raw if d]

    all_drivers = _all_drivers_for_picker()
    selected_drivers = [
        d for d in all_drivers if d["driver_id"] in set(driver_ids)
    ]

    trips, warning = [], ""
    if driver_ids:
        try:
            trips = th_svc.trips_by_driver(driver_ids, begin, end)
        except RateLimitExceeded:
            warning = ("Tracksolid daily API limit reached. Try again later.")
        except Exception as exc:
            log.exception("trip_history driver view failed")
            warning = f"Could not fetch trips: {exc}"

    truck_counts = {}
    for t in trips:
        k = t["truck_id"] or t["imei"]
        if k:
            truck_counts[k] = truck_counts.get(k, 0) + 1

    return render_template(
        "trip_history/by_driver.html",
        start=start_s,
        end=end_s,
        driver_ids=driver_ids,
        selected_drivers=selected_drivers,
        all_drivers=all_drivers,
        trips=trips,
        truck_counts=truck_counts,
        warning=warning,
        is_admin=_is_admin(),
    )


# ---------------------------------------------------------------------------
# By truck
# ---------------------------------------------------------------------------

@bp.route("/by-truck")
@login_required
def by_truck():
    _require_view()

    begin, end, start_s, end_s, _rng = _resolve_range(request.args)

    imei_raw = request.args.getlist("imei")
    imeis = [i for i in imei_raw if i]

    all_trucks = _all_trucks_for_picker()
    by_imei = {t["imei"]: t for t in all_trucks}
    selected_trucks = [by_imei[i] for i in imeis if i in by_imei]

    trips, warning = [], ""
    if imeis:
        try:
            trips = th_svc.trips_by_range(begin, end, imeis=imeis)
        except RateLimitExceeded:
            warning = ("Tracksolid daily API limit reached. Try again later.")
        except Exception as exc:
            log.exception("trip_history truck view failed")
            warning = f"Could not fetch trips: {exc}"

    summary = {}
    for t in trips:
        k = t["imei"]
        s = summary.setdefault(k, {
            "imei": k,
            "truck_id": t["truck_id"] or by_imei.get(k, {}).get("name", ""),
            "trips": 0,
            "km": 0.0,
            "seconds": 0,
        })
        s["trips"]  += 1
        if t["distance"]:
            try:
                s["km"] += float(t["distance"])
            except (TypeError, ValueError):
                pass
        s["seconds"] += int(t["duration_s"] or 0)

    summary_rows = sorted(summary.values(),
                          key=lambda s: s["trips"], reverse=True)

    return render_template(
        "trip_history/by_truck.html",
        start=start_s,
        end=end_s,
        imeis=imeis,
        selected_trucks=selected_trucks,
        all_trucks=all_trucks,
        trips=trips,
        summary=summary_rows,
        warning=warning,
        is_admin=_is_admin(),
    )