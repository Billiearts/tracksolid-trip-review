"""
blueprints/drivers.py — driver registry, truck assignment, and
driver-first trip search.

Permissions
-----------
* `drivers_view`   — see the drivers list and search trips by driver.
* `drivers_add`    — register new drivers.
* `drivers_edit`   — edit / delete existing drivers.
* `drivers_assign` — assign / release a truck.
* `drivers_override_rest` — bypass the driver rest-period guard.

Admins and super-admins always have all of the above.

Assignment rules
----------------
There is NO truck-state (open-trip / fence) guard on assignment. A
truck may be assigned at any time as long as it is not already held by
a different driver (an open `truck_assignments` row). Operational
trucks (e.g. service/pickup devices) are excluded from the picker,
matching the classification used by the report generator.

Driver rest period
------------------
After a driver's assignment is released (manual OR auto on trip end),
they must rest for `driver_rest_settings.rest_period_hours` hours
before they can be assigned to another truck. Admins and holders of
`drivers_override_rest` may bypass.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    jsonify, session, abort,
)

from decorators import login_required
from services import audit
from services import drivers as drivers_svc
from services.ratelimit import is_rate_limited
from jimi import RateLimitExceeded


log = logging.getLogger(__name__)
bp = Blueprint("drivers", __name__, url_prefix="/drivers")


_TRIPS_WORKERS = 4
_trips_pool = ThreadPoolExecutor(max_workers=_TRIPS_WORKERS,
                                 thread_name_prefix="drivers-trips")


# ---------------------------------------------------------------------------
# Access helpers
# ---------------------------------------------------------------------------

def _has_role(role: str) -> bool:
    if session.get("role") in ("admin", "super_admin"):
        return True
    return role in (session.get("extra_roles") or [])


def _can_view() -> bool:
    return _has_role("drivers_view")


def _can_add() -> bool:
    return _has_role("drivers_add")


def _can_edit() -> bool:
    return _has_role("drivers_edit")


def _can_assign() -> bool:
    return _has_role("drivers_assign")


def _can_override_rest() -> bool:
    if session.get("role") in ("admin", "super_admin"):
        return True
    return "drivers_override_rest" in (session.get("extra_roles") or [])


def _is_admin() -> bool:
    return session.get("role") in ("admin", "super_admin")


def _require_view():
    if not _can_view():
        abort(403)


def _require_add():
    if not _can_add():
        abort(403)


def _require_edit():
    if not _can_edit():
        abort(403)


def _require_assign():
    if not _can_assign():
        abort(403)


def _caller_id() -> str | None:
    return session.get("user_id")


def _svc_client():
    from services.supabase_client import admin_client
    return admin_client()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=timezone.utc)


def _parse_requested_drivers() -> list[dict] | None:
    import json as _json
    raw = request.form.get("drivers") or "[]"
    try:
        items = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    out = []
    seen = set()
    for it in items:
        if isinstance(it, str):
            did = it.strip()
            name = ""
        elif isinstance(it, dict):
            did = (it.get("driver_id") or it.get("id") or "").strip()
            name = (it.get("full_name") or it.get("name") or "").strip()
        else:
            continue
        if not did or did in seen:
            continue
        seen.add(did)
        out.append({"driver_id": did, "full_name": name})
    return out


def _role_choices() -> list[dict]:
    """Human-readable role options for the new/edit <select>."""
    return [
        {"value": "driver",       "label": "Driver"},
        {"value": "chief_driver", "label": "Chief driver"},
        {"value": "trainee",      "label": "Trainee"},
    ]


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------

@bp.route("/")
@login_required
def index():
    _require_view()
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    search = (request.args.get("q") or "").strip()
    include_inactive = request.args.get("inactive") == "1"
    include_deleted = _is_admin() and request.args.get("deleted") == "1"

    view_filter = (request.args.get("view") or "all").strip()
    if view_filter not in ("all", "assigned", "unassigned"):
        view_filter = "all"

    role_filter = (request.args.get("role") or "all").strip()
    if role_filter not in ("all",) + tuple(drivers_svc.DRIVER_ROLES):
        role_filter = "all"

    try:
        result = drivers_svc.list_drivers(
            search=search, page=page, per_page=50,
            include_inactive=include_inactive,
            include_deleted=include_deleted,
            view=view_filter,
            driver_role=(None if role_filter == "all" else role_filter),
        )
    except drivers_svc.DriverError as exc:
        flash(str(exc), "error")
        result = {"drivers": [], "page": 1, "per_page": 50,
                  "total": 0, "has_more": False}

    for d in result["drivers"]:
        d["current_assignment"] = drivers_svc.current_assignment_for_driver(
            d["id"])

    return render_template(
        "drivers/index.html",
        result=result, search=search,
        include_inactive=include_inactive,
        include_deleted=include_deleted,
        view_filter=view_filter,
        role_filter=role_filter,
        role_choices=_role_choices(),
        can_add=_can_add(),
        can_edit=_can_edit(),
        can_assign=_can_assign(),
        is_admin=_is_admin(),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new_driver():
    _require_add()
    if request.method == "POST":
        try:
            created = drivers_svc.create_driver(
                full_name=(request.form.get("full_name") or "").strip(),
                staff_id=(request.form.get("staff_id") or "").strip() or None,
                phone=(request.form.get("phone") or "").strip() or None,
                license_number=(request.form.get("license_number") or "").strip() or None,
                license_expiry=(request.form.get("license_expiry") or "").strip() or None,
                active=request.form.get("active") != "off",
                notes=(request.form.get("notes") or "").strip() or None,
                driver_role=(request.form.get("driver_role") or "driver").strip(),
                created_by=_caller_id(),
            )
        except drivers_svc.DriverError as exc:
            flash(str(exc), "error")
            return render_template(
                "drivers/new.html",
                form=request.form,
                role_choices=_role_choices(),
            )

        audit.record(
            "driver.create",
            target=created["full_name"],
            detail=(f"staff_id={created.get('staff_id') or ''}, "
                    f"role={created.get('driver_role')}"),
            meta={"driver_id": created["id"],
                  "staff_id": created.get("staff_id"),
                  "driver_role": created.get("driver_role")},
        )
        flash(f"Driver {created['full_name']} created.", "success")
        return redirect(url_for("drivers.index"))

    return render_template(
        "drivers/new.html",
        form={},
        role_choices=_role_choices(),
    )


@bp.route("/<driver_id>/edit", methods=["GET", "POST"])
@login_required
def edit_driver(driver_id):
    _require_edit()
    driver = drivers_svc.get_driver(driver_id)
    if not driver or driver.get("deleted_at"):
        flash("Driver not found.", "error")
        return redirect(url_for("drivers.index"))

    if request.method == "POST":
        try:
            updated = drivers_svc.update_driver(
                driver_id,
                full_name=(request.form.get("full_name") or "").strip(),
                staff_id=(request.form.get("staff_id") or "").strip() or None,
                phone=(request.form.get("phone") or "").strip() or None,
                license_number=(request.form.get("license_number") or "").strip() or None,
                license_expiry=(request.form.get("license_expiry") or "").strip() or None,
                active=request.form.get("active") != "off",
                notes=(request.form.get("notes") or "").strip() or None,
                driver_role=(request.form.get("driver_role") or "driver").strip(),
            )
        except drivers_svc.DriverError as exc:
            flash(str(exc), "error")
            return render_template(
                "drivers/edit.html",
                driver=driver,
                form=request.form,
                role_choices=_role_choices(),
            )

        audit.record_diff(
            "driver.update",
            target=updated["full_name"],
            before=driver, after=updated,
            meta={"driver_id": driver_id},
        )
        flash("Driver updated.", "success")
        return redirect(url_for("drivers.detail", driver_id=driver_id))

    return render_template(
        "drivers/edit.html",
        driver=driver,
        form={},
        role_choices=_role_choices(),
    )


@bp.route("/<driver_id>/delete", methods=["POST"])
@login_required
def delete_driver(driver_id):
    _require_edit()
    driver = drivers_svc.get_driver(driver_id)
    if not driver or driver.get("deleted_at"):
        flash("Driver not found.", "error")
        return redirect(url_for("drivers.index"))
    try:
        drivers_svc.soft_delete_driver(driver_id)
    except drivers_svc.DriverError as exc:
        flash(str(exc), "error")
        return redirect(url_for("drivers.detail", driver_id=driver_id))
    audit.record(
        "driver.delete", target=driver["full_name"],
        detail="soft delete (assignments preserved)",
        meta={"driver_id": driver_id},
    )
    flash(f"Driver {driver['full_name']} deleted. "
          f"Historical assignments are preserved.", "success")
    return redirect(url_for("drivers.index"))


@bp.route("/<driver_id>")
@login_required
def detail(driver_id):
    _require_view()
    driver = drivers_svc.get_driver(driver_id)
    if not driver:
        flash("Driver not found.", "error")
        return redirect(url_for("drivers.index"))
    if driver.get("deleted_at") and not _is_admin():
        flash("Driver not found.", "error")
        return redirect(url_for("drivers.index"))

    assignments = drivers_svc.list_assignments(driver_id=driver_id, limit=500)
    current = next((a for a in assignments if a["released_at"] is None), None)

    rest_until_dt = None
    rest_hours = 0.0
    if not current:
        try:
            rest_until_dt = drivers_svc.rest_until(driver_id)
            rest_hours = drivers_svc.get_rest_period_hours()
        except Exception:
            log.exception("detail: rest-period lookup failed for %s",
                          driver_id)

    return render_template(
        "drivers/detail.html",
        driver=driver,
        assignments=assignments,
        current=current,
        rest_until=rest_until_dt,
        rest_period_hours=rest_hours,
        can_edit=_can_edit(),
        can_assign=_can_assign(),
        can_override_rest=_can_override_rest(),
        is_admin=_is_admin(),
    )


# ---------------------------------------------------------------------------
# Rest-period settings
# ---------------------------------------------------------------------------

@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if not _is_admin():
        abort(403)

    if request.method == "POST":
        raw = (request.form.get("rest_period_hours") or "").strip()
        try:
            hours = float(raw)
        except ValueError:
            flash("Rest period must be a number between 0 and 72.", "error")
            return redirect(url_for("drivers.settings"))
        if hours < 0 or hours > 72:
            flash("Rest period must be between 0 and 72 hours.", "error")
            return redirect(url_for("drivers.settings"))
        try:
            drivers_svc.set_rest_period_hours(hours, updated_by=_caller_id())
        except drivers_svc.DriverError as exc:
            flash(str(exc), "error")
            return redirect(url_for("drivers.settings"))
        audit.record(
            "driver.rest_settings_update",
            target="driver_rest_settings",
            detail=f"rest_period_hours={hours}",
            meta={"rest_period_hours": hours},
        )
        flash("Rest period updated.", "success")
        return redirect(url_for("drivers.settings"))

    try:
        hours = drivers_svc.get_rest_period_hours()
    except drivers_svc.DriverError as exc:
        flash(str(exc), "error")
        hours = 8.0
    return render_template(
        "drivers/settings.html",
        rest_period_hours=hours,
    )


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

@bp.route("/<driver_id>/assign", methods=["POST"])
@login_required
def assign(driver_id):
    _require_assign()
    driver = drivers_svc.get_driver(driver_id)
    if not driver or driver.get("deleted_at"):
        flash("Driver not found.", "error")
        return redirect(url_for("drivers.index"))

    truck_id = (request.form.get("truck_id") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None
    want_rest_override = request.form.get("override_rest") == "on"

    if not truck_id:
        flash("Please choose a truck.", "error")
        return redirect(url_for("drivers.detail", driver_id=driver_id))

    override_rest = want_rest_override and _can_override_rest()

    try:
        a = drivers_svc.assign_driver(
            driver_id=driver_id, truck_id=truck_id,
            notes=notes, created_by=_caller_id(),
            override_rest=override_rest,
        )
    except drivers_svc.DriverError as exc:
        flash(str(exc), "error")
        return redirect(url_for("drivers.detail", driver_id=driver_id))

    audit.record(
        "driver.assign",
        target=driver["full_name"],
        detail=(f"truck={truck_id}"
                + (" [override: rest period]" if override_rest else "")),
        meta={"driver_id": driver_id, "assignment_id": a["id"],
              "truck_id": truck_id, "imei": a.get("imei"),
              "assigned_by": _caller_id(),
              "override_rest": override_rest},
        severity="warning" if override_rest else "info",
    )
    flash(f"Assigned {driver['full_name']} to {truck_id}.", "success")
    return redirect(url_for("drivers.detail", driver_id=driver_id))


@bp.route("/assignments/<assignment_id>/release", methods=["POST"])
@login_required
def release(assignment_id):
    _require_assign()
    try:
        a = drivers_svc.release_assignment(assignment_id, reason="manual")
    except drivers_svc.DriverError as exc:
        flash(str(exc), "error")
        return redirect(url_for("drivers.index"))

    audit.record(
        "driver.release",
        target=a["truck_id"] or a["imei"],
        detail=f"driver={a['driver_id']}",
        meta={"assignment_id": assignment_id,
              "driver_id": a["driver_id"], "imei": a["imei"],
              "reason": "manual",
              "released_by": _caller_id()},
    )
    flash("Assignment released.", "success")
    return redirect(url_for("drivers.detail", driver_id=a["driver_id"]))


# ---------------------------------------------------------------------------
# Public API for the search combobox
# ---------------------------------------------------------------------------

@bp.route("/api/list")
@login_required
def api_list():
    if not _can_view():
        abort(403)
    include_inactive = request.args.get("inactive") == "1"
    search = (request.args.get("q") or "").strip()
    try:
        result = drivers_svc.list_drivers(
            search=search, page=1, per_page=200,
            include_inactive=include_inactive,
        )
    except drivers_svc.DriverError as exc:
        return jsonify({"error": str(exc)}), 502

    out = []
    for d in result["drivers"]:
        cur = drivers_svc.current_assignment_for_driver(d["id"])
        out.append({
            "driver_id":     d["id"],
            "full_name":     d["full_name"],
            "staff_id":      d["staff_id"],
            "active":        d["active"],
            "driver_role":   d.get("driver_role") or "driver",
            "current_truck": cur["truck_id"] if cur else "",
        })
    return jsonify({"drivers": out})


@bp.route("/api/available_trucks")
@login_required
def api_available_trucks():
    """Trucks a driver can be assigned to.

    Only **unassigned regular** trucks are returned. Operational trucks
    are excluded using the same classification as the report generator.

    The endpoint always attempts a live fetch when the cache is empty,
    so the caller is never told "warming up" forever. If Tracksolid
    itself is unreachable, we return a real error message and
    `degraded=true` so the UI can offer a retry.
    """
    from services import devices as devices_svc

    if not _can_view():
        log.warning("available_trucks: caller lacks drivers_view "
                    "(role=%s, extra=%s)",
                    session.get("role"),
                    session.get("extra_roles"))
        return jsonify({
            "trucks": [],
            "is_admin": _is_admin(),
            "can_assign": False,
            "degraded": True,
            "message": "You do not have permission to view the truck list.",
        }), 200

    # 1. Trucks already assigned to some driver -> never offer them.
    assigned_trucks: set[str] = set()
    try:
        resp = (_svc_client().table("truck_assignments")
                .select("truck_id")
                .is_("released_at", "null")
                .limit(2000)
                .execute())
        for r in (resp.data or []):
            tid = r.get("truck_id")
            if tid:
                assigned_trucks.add(tid)
    except Exception as exc:
        log.exception("available_trucks: assignment lookup failed: %s", exc)

    # 2. Load the device list. Prefer the cache; on miss, fetch live
    #    with a bounded retry so a single RemoteDisconnected doesn't
    #    strand the UI in "warming up" forever.
    devices = None
    fetch_error = None
    try:
        devices = devices_svc.get_devices_cached_only()
    except AttributeError:
        log.warning(
            "available_trucks: get_devices_cached_only() missing; "
            "falling back to live get_devices().")
        devices = None
    except Exception:
        log.exception("available_trucks: cached lookup raised")
        devices = None

    if devices is None:
        import time
        last_exc = None
        for attempt in range(3):
            try:
                devices = devices_svc.get_devices()
                if devices:
                    break
                last_exc = RuntimeError("get_devices returned empty")
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "available_trucks: get_devices attempt %d/3 failed: %s",
                    attempt + 1, exc,
                )
            if attempt < 2:
                time.sleep(1 + attempt)  # 1 s, 2 s

        if not devices:
            fetch_error = (
                str(last_exc) if last_exc
                else "Tracksolid returned an empty device list."
            )
            log.error("available_trucks: live fetch failed: %s", fetch_error)

    # 3. Fetch failed -> tell the user (don't lie about "warming up").
    if devices is None or not devices:
        return jsonify({
            "trucks": [],
            "is_admin": _is_admin(),
            "can_assign": _can_assign(),
            "degraded": True,
            "retry":    True,
            "message": (
                "Could not load trucks from Tracksolid"
                + (f": {fetch_error}" if fetch_error else ".")
                + " Check the network, then retry."
            ),
        })

    # 4. Filter and shape the list.
    out = []
    seen = set()
    for d in devices:
        name = (d.get("deviceName") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)

        if name in assigned_trucks:
            continue

        try:
            category = devices_svc.classify_device(name)
        except Exception:
            category = "regular"
        if category != "regular":
            continue

        out.append({
            "truck_id":  name,
            "name":      name,
            "imei":      d.get("imei") or "",
            "category":  category,
        })

    out.sort(key=lambda x: x["name"].lower())
    return jsonify({
        "trucks": out,
        "is_admin": _is_admin(),
        "can_assign": _can_assign(),
        "degraded": False,
    })


# ---------------------------------------------------------------------------
# Driver-first trip search
# ---------------------------------------------------------------------------

@bp.route("/trips", methods=["POST"])
@login_required
def trips_list():
    if not _can_view():
        abort(403)

    drivers = _parse_requested_drivers()
    begin_s = (request.form.get("begin") or "").strip()
    end_s   = (request.form.get("end") or "").strip()
    include_history = request.form.get("include_history") == "on"
    require_assignment = request.form.get("require_assignment") != "off"

    if not drivers:
        flash("Please select at least one driver.", "error")
        return redirect(url_for("index"))
    if not begin_s or not end_s:
        flash("Begin and end dates are required.", "error")
        return redirect(url_for("index"))

    try:
        begin = _parse_dt(begin_s)
        end   = _parse_dt(end_s)
    except ValueError:
        flash("Dates must be 'YYYY-MM-DD HH:MM:SS'.", "error")
        return redirect(url_for("index"))

    audit.record(
        "search.trips_by_driver",
        target=", ".join(
            d["full_name"] or d["driver_id"] for d in drivers
        )[:200],
        detail=(f"drivers={len(drivers)}, begin={begin_s}, end={end_s}, "
                f"include_history={include_history}, "
                f"require_assignment={require_assignment}"),
        meta={"driver_count": len(drivers),
              "include_history": include_history,
              "require_assignment": require_assignment},
    )

    resolved: list[dict] = []
    warnings: list[str] = []
    for d in drivers:
        info = drivers_svc.get_driver(d["driver_id"])
        if not info:
            warnings.append(
                f"Driver {d['full_name'] or d['driver_id']} not found.")
            continue
        trucks = drivers_svc.trucks_for_driver_during(info["id"], begin, end)
        if not trucks:
            if require_assignment:
                warnings.append(
                    f"{info['full_name']} had no truck assignments between "
                    f"{begin_s} and {end_s}."
                )
                continue
            cur = drivers_svc.current_assignment_for_driver(info["id"])
            if cur:
                trucks = [{
                    "truck_id": cur["truck_id"],
                    "imei":     cur["imei"],
                    "windows":  [(cur["assigned_at"], cur["released_at"])],
                }]
            else:
                warnings.append(
                    f"{info['full_name']} has no assignments and no current "
                    f"truck; nothing to search."
                )
                continue
        resolved.append({**info, "trucks": trucks})

    if not resolved:
        for w in warnings:
            flash(w, "error")
        return redirect(url_for("index"))

    from report import find_trips

    unique_trucks: dict[str, dict] = {}
    for r in resolved:
        for t in r["trucks"]:
            unique_trucks.setdefault(t["truck_id"], {
                "truck_id": t["truck_id"],
                "imei":     t["imei"],
            })

    def _safe(imei: str):
        if is_rate_limited():
            raise RateLimitExceeded("rate limited (short-circuit)")
        return find_trips(imei, begin, end)

    futures = {_trips_pool.submit(_safe, info["imei"]): info["truck_id"]
               for info in unique_trucks.values() if info.get("imei")}
    trips_by_truck: dict[str, list[dict]] = {}
    rate_limited = False
    for f in as_completed(futures):
        truck_id = futures[f]
        try:
            trips_by_truck[truck_id] = f.result() or []
        except RateLimitExceeded:
            rate_limited = True
        except Exception as exc:
            log.exception("find_trips failed for %s", truck_id)
            warnings.append(f"{truck_id}: {type(exc).__name__}")

    if rate_limited:
        flash("Tracksolid Pro daily API limit reached. Try again later.",
              "error")
        return redirect(url_for("index"))

    groups: list[dict] = []
    total = 0
    for r in resolved:
        driver_trips: list[dict] = []
        for t in r["trucks"]:
            truck_id = t["truck_id"]
            windows = t["windows"]
            for trip in trips_by_truck.get(truck_id, []):
                start = trip.get("start")
                if require_assignment and not _trip_in_windows(start,
                                                               windows):
                    continue
                driver_trips.append({
                    **trip,
                    "truck":       truck_id,
                    "imei":        t["imei"],
                    "driver_id":   r["id"],
                    "driver_name": r["full_name"],
                })
        if not driver_trips:
            warnings.append(
                f"No trips for {r['full_name']} in that window.")
            continue
        driver_trips.sort(key=lambda x: x["start"])
        groups.append({
            "driver_id":   r["id"],
            "driver_name": r["full_name"],
            "truck":       _truck_summary(driver_trips),
            "imei":        (driver_trips[0]["imei"]
                            if driver_trips and
                               len({t["imei"] for t in driver_trips}) == 1
                            else ""),
            "trips":       driver_trips,
        })
        total += len(driver_trips)

    if not groups:
        for w in warnings:
            flash(w, "error")
        return redirect(url_for("index"))

    groups.sort(key=lambda g: g["driver_name"].lower())

    return render_template(
        "trips.html",
        truck_groups=groups,
        total_trips=total,
        not_found=[],
        begin_str=begin_s,
        end_str=end_s,
        include_history=include_history,
        group_by="driver",
        warnings=warnings,
    )


def _trip_in_windows(trip_start, windows: list[tuple]) -> bool:
    if trip_start is None:
        return False
    for s, e in windows:
        if s is None:
            continue
        if trip_start < s:
            continue
        if e is not None and trip_start >= e:
            continue
        return True
    return False


def _truck_summary(trips: list[dict]) -> str:
    trucks = sorted({t["truck"] for t in trips if t.get("truck")})
    if not trucks:
        return ""
    if len(trucks) == 1:
        return trucks[0]
    return f"{len(trucks)} trucks"