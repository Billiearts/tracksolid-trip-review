"""
services/drivers.py — driver registry and truck-assignment persistence.

Drivers are persistent identities. Truck assignments are time-bounded rows
in `truck_assignments`; releasing one never deletes it.

Semantics
---------
* Soft-deleting a driver (deleted_at) removes them from list views but
  leaves all their historic assignments intact. No new assignments can be
  made to a deleted driver.
* A truck currently on a trip cannot be reassigned without
  override_active_trip=True (admins / super-admins only).
* When a trip finishes (its end is known), `auto_release_finished_assignments`
  releases the driver from the truck with released_reason="trip_ended", so
  the next dispatch has to re-assign.
* Truck-state guard: a truck may only be assigned when its current fence
  state is inside the yard. The yard is not a single fence — it's a group
  (AWASO, the wrapper fences, and the station fences). A truck inside
  TRIP TIME is blocked as "on an open trip"; a truck outside both the
  yard and TRIP TIME is blocked as "not at the yard".
  See `current_fence_state()` below.

Driver positions
----------------
`driver_role` is one of 'driver', 'chief_driver', 'trainee' — enforced by
a CHECK constraint in the DB and validated here. Use `DRIVER_ROLES` for
the allowed set.

Assignment actor
----------------
Every assignment records `created_by` (the auth user who made it). The
list functions resolve this to an email via a nested select so the UI
can show "assigned by X" without a second round-trip per row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from services.supabase_client import admin_client


log = logging.getLogger(__name__)

# Yard fences — the guard treats "inside any of these" as "at the yard".
# Imported lazily in _fence_matches_yard() to avoid a circular import
# at module load (report_analysis imports jimi, which imports config).


class DriverError(RuntimeError):
    pass


# Allowed driver positions. Kept in sync with the CHECK constraint in
# sql/2026_11_23_driver_roles.sql.
DRIVER_ROLES = ("driver", "chief_driver", "trainee")
_ROLE_LABELS = {
    "driver":       "Driver",
    "chief_driver": "Chief driver",
    "trainee":      "Trainee",
}


# Fences used by the truck-state guard. TRIP TIME is matched by geoid
# (authoritative) with a name fallback; the yard is matched by name
# against the AWASO fences plus the wrapper/bracket/station fences from
# report_analysis.
_TRIP_TIME_FENCE_NAME_FALLBACKS = ("TRIP TIME GBC", "GBC TRIP TIME")
_AWASO_FENCE_NAMES = frozenset({"GBC AWASO", "GBC AWASO 2"})
_FENCE_LOOKBACK_HOURS = 72


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _client():
    return admin_client()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    dt = _utc(dt)
    return dt.isoformat() if dt else None


def _blank(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _normalise_role(v) -> str:
    """Coerce any input to a valid DRIVER_ROLES value."""
    s = (str(v).strip().lower() if v is not None else "") or "driver"
    if s not in DRIVER_ROLES:
        s = "driver"
    return s


def _resolve_imei_for_truck(truck_id: str) -> str | None:
    """truck display name -> device IMEI.

    Single source of truth used by both the open-trip guard and the
    assignment write. The blueprint never sends an IMEI, so this is
    the only path that decides which device backs a truck.
    """
    tid = _blank(truck_id)
    if not tid:
        return None
    try:
        from services.devices import find_device
        dev = find_device(tid)
    except Exception:
        log.exception("_resolve_imei_for_truck: find_device failed for %r",
                      tid)
        return None
    if not dev:
        return None
    return dev.get("imei") or None


def _parse_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return _utc(v)
    try:
        return _utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


def _row_to_driver(row: dict) -> dict:
    raw = row.get("driver_role")
    role = (str(raw).strip().lower() if raw is not None else "") or "driver"
    if role not in DRIVER_ROLES:
        # Unknown/non-null value is a data problem — don't silently
        # coerce to "driver", log it and fall back so the row still
        # renders but the issue is visible in logs.
        log.warning("_row_to_driver: unrecognised driver_role %r for id %r",
                    raw, row.get("id"))
        role = "driver"
    return {
        "id":             row.get("id"),
        "full_name":      row.get("full_name") or "",
        "staff_id":       row.get("staff_id") or row.get("employee_id") or "",
        "phone":          row.get("phone") or "",
        "license_number": row.get("license_number") or "",
        "license_expiry": row.get("license_expiry"),
        "active":         bool(row.get("active", True)),
        "notes":          row.get("notes") or "",
        "driver_role":    role,
        "driver_role_label": _ROLE_LABELS[role],
        "created_at":     _parse_dt(row.get("created_at")),
        "updated_at":     _parse_dt(row.get("updated_at")),
        "deleted_at":     _parse_dt(row.get("deleted_at")),
    }


def _row_to_assignment(row: dict) -> dict:
    """Normalise an assignment row.

    `creator` is the nested select of profiles on created_by — a dict
    with email/full_name when present, or None. We surface it as
    `assigned_by_email` / `assigned_by_name` so templates don't have
    to reach into nested dicts.
    """
    creator = row.get("creator") or {}
    if isinstance(creator, list):
        creator = creator[0] if creator else {}
    if not isinstance(creator, dict):
        creator = {}

    return {
        "id":                  row.get("id"),
        "driver_id":           row.get("driver_id"),
        "imei":                row.get("imei") or "",
        "truck_id":            row.get("truck_id") or "",
        "assigned_at":         _parse_dt(row.get("assigned_at")),
        "released_at":         _parse_dt(row.get("released_at")),
        "released_reason":     row.get("released_reason") or "",
        "notes":               row.get("notes") or "",
        "created_by":          row.get("created_by"),
        "assigned_by_email":   creator.get("email") or "",
        "assigned_by_name":    creator.get("full_name") or "",
    }


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def list_drivers(*, search: str = "", page: int = 1, per_page: int = 50,
                 include_inactive: bool = False,
                 include_deleted: bool = False,
                 view: str = "all",
                 driver_role: str | None = None) -> dict:
    """List drivers, optionally filtered by open-assignment status.

    `view` is one of:
      * "all"        — every driver matching the other filters (default)
      * "assigned"   — drivers with an open truck_assignment
      * "unassigned" — drivers with no open truck_assignment

    `driver_role`, when set to a value in DRIVER_ROLES, adds a second
    filter. Both filters compose: e.g. `view="assigned",
    driver_role="chief_driver"` returns assigned chief drivers.
    """
    page = max(1, int(page or 1))
    per_page = max(1, min(200, int(per_page or 50)))
    view = view if view in ("all", "assigned", "unassigned") else "all"
    role_filter = None
    if driver_role in DRIVER_ROLES:
        role_filter = driver_role

    table = "drivers" if view == "all" else "drivers_open_status"
    q = _client().table(table).select("*", count="exact")

    if not include_deleted:
        q = q.is_("deleted_at", "null")
    if not include_inactive:
        q = q.eq("active", True)
    if role_filter:
        q = q.eq("driver_role", role_filter)

    s = _blank(search)
    if s:
        safe = s.replace(",", " ").replace("%", "")
        q = q.or_(
            f"full_name.ilike.%{safe}%,staff_id.ilike.%{safe}%"
        )

    if view == "assigned":
        q = q.not_.is_("open_assignment_truck", "null")
    elif view == "unassigned":
        q = q.is_("open_assignment_truck", "null")

    offset = (page - 1) * per_page
    q = q.order("full_name", desc=False).range(offset, offset + per_page - 1)

    try:
        resp = q.execute()
    except Exception as exc:
        log.exception("list_drivers failed")
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc

    rows = [_row_to_driver(r) for r in (resp.data or [])]
    total = int(getattr(resp, "count", 0) or 0)
    return {
        "drivers":  rows,
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "has_more": (page * per_page) < total,
    }


def get_driver(driver_id: str) -> dict | None:
    if not driver_id:
        return None
    try:
        resp = (_client().table("drivers")
                .select("*").eq("id", driver_id).limit(1).execute())
    except Exception:
        log.exception("get_driver failed for %s", driver_id)
        return None
    rows = resp.data or []
    return _row_to_driver(rows[0]) if rows else None


def get_driver_by_staff_id(staff_id: str) -> dict | None:
    sid = _blank(staff_id)
    if not sid:
        return None
    try:
        resp = (_client().table("drivers").select("*")
                .ilike("staff_id", sid)
                .is_("deleted_at", "null").limit(1).execute())
    except Exception:
        log.exception("get_driver_by_staff_id failed for %s", sid)
        return None
    rows = resp.data or []
    return _row_to_driver(rows[0]) if rows else None


def find_driver_by_name(full_name: str) -> dict | None:
    name = _blank(full_name)
    if not name:
        return None
    try:
        resp = (_client().table("drivers").select("*")
                .ilike("full_name", name)
                .is_("deleted_at", "null").limit(1).execute())
    except Exception:
        log.exception("find_driver_by_name failed for %r", name)
        return None
    rows = resp.data or []
    return _row_to_driver(rows[0]) if rows else None


def create_driver(*, full_name: str, staff_id: str | None = None,
                  phone: str | None = None,
                  license_number: str | None = None,
                  license_expiry: str | None = None,
                  active: bool = True, notes: str | None = None,
                  driver_role: str = "driver",
                  created_by: str | None = None) -> dict:
    name = _blank(full_name)
    if not name:
        raise DriverError("Driver full name is required.")
    sid = _blank(staff_id)
    if sid and get_driver_by_staff_id(sid):
        raise DriverError(f"Staff ID {sid!r} is already in use.")

    payload = {
        "full_name":      name,
        "staff_id":       sid,
        "phone":          _blank(phone),
        "license_number": _blank(license_number),
        "license_expiry": _blank(license_expiry),
        "active":         bool(active),
        "notes":          _blank(notes),
        "driver_role":    _normalise_role(driver_role),
        "created_by":     created_by,
    }
    try:
        resp = _client().table("drivers").insert(payload).execute()
    except Exception as exc:
        log.exception("create_driver failed")
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc
    rows = resp.data or []
    if not rows:
        raise DriverError("Database did not return the created driver.")
    return _row_to_driver(rows[0])


def update_driver(driver_id: str, **fields) -> dict:
    if not driver_id:
        raise DriverError("Missing driver id.")

    allowed = {"full_name", "staff_id", "phone", "license_number",
               "license_expiry", "active", "notes", "driver_role"}
    payload = {}
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "full_name":
            name = _blank(v)
            if not name:
                raise DriverError("Driver full name is required.")
            payload[k] = name
        elif k == "active":
            payload[k] = bool(v)
        elif k == "driver_role":
            payload[k] = _normalise_role(v)
        elif k == "staff_id":
            sid = _blank(v)
            if sid:
                other = get_driver_by_staff_id(sid)
                if other and other["id"] != driver_id:
                    raise DriverError(f"Staff ID {sid!r} is already in use.")
            payload[k] = sid
        else:
            payload[k] = _blank(v)

    if not payload:
        existing = get_driver(driver_id)
        if not existing:
            raise DriverError("Driver not found.")
        return existing

    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        resp = (_client().table("drivers").update(payload)
                .eq("id", driver_id).execute())
    except Exception as exc:
        log.exception("update_driver failed for %s", driver_id)
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc
    rows = resp.data or []
    if not rows:
        raise DriverError("Driver not found.")
    return _row_to_driver(rows[0])


def soft_delete_driver(driver_id: str) -> None:
    """Mark the driver deleted. Historical assignments are left intact."""
    if not driver_id:
        raise DriverError("Missing driver id.")
    now = datetime.now(timezone.utc).isoformat()
    try:
        _client().table("drivers").update({
            "deleted_at": now,
            "updated_at": now,
            "active":     False,
        }).eq("id", driver_id).execute()
    except Exception as exc:
        log.exception("soft_delete_driver failed for %s", driver_id)
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

_ASSIGNMENT_SELECT = (
    "id,driver_id,imei,truck_id,assigned_at,released_at,"
    "released_reason,notes,created_by,"
    "creator:created_by(email,full_name)"
)


def list_assignments(*, driver_id: str | None = None,
                     imei: str | None = None,
                     active_only: bool = False,
                     limit: int = 200) -> list[dict]:
    q = (_client().table("truck_assignments")
         .select(_ASSIGNMENT_SELECT))
    if driver_id:
        q = q.eq("driver_id", driver_id)
    if imei:
        q = q.eq("imei", imei)
    if active_only:
        q = q.is_("released_at", "null")
    q = q.order("assigned_at", desc=True).limit(max(1, min(2000, limit)))
    try:
        resp = q.execute()
    except Exception:
        log.exception("list_assignments failed")
        return []
    return [_row_to_assignment(r) for r in (resp.data or [])]


def current_assignment_for_imei(imei: str) -> dict | None:
    if not imei:
        return None
    rows = list_assignments(imei=imei, active_only=True, limit=1)
    return rows[0] if rows else None


def current_assignment_for_driver(driver_id: str) -> dict | None:
    if not driver_id:
        return None
    rows = list_assignments(driver_id=driver_id, active_only=True, limit=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Truck-state guard — fence-based
# ---------------------------------------------------------------------------

def _fence_matches_trip_time(alarm: dict) -> bool:
    try:
        from report_analysis import GBC_GEOIDS, _alarm_fence_name
    except Exception:
        return False
    geoid = (alarm.get("geoid") or "").strip()
    if geoid in GBC_GEOIDS:
        return True
    name = _alarm_fence_name(alarm)
    return any(fb in name for fb in _TRIP_TIME_FENCE_NAME_FALLBACKS)


def _fence_matches_yard(alarm: dict) -> bool:
    """True if the alarm's fence is anywhere inside the yard.

    The yard is not one fence — it's a group. A truck is 'at the yard'
    whenever its last in/out event is one of:
      * GBC AWASO / GBC AWASO 2
      * the wrapper fences (HAULAGE TRUCKS EXIT, GBC EXITED,
        FUEL AND WAYBILL, LOADED AND PARKED)
      * any of the YARD_STATIONS (MAINTENANCE / WAYBILL / FUELING /
        LOADING / WEIGHING / WAITING TIME)

    Using only AWASO (the original rule) missed trucks like GT 8131 -25,
    which sit in the yard but never trigger the AWASO fence. This is
    why the assign form said 'not at the yard' while the truck was
    visibly inside.
    """
    try:
        from report_analysis import (
            _alarm_fence_name,
            YARD_WRAPPER_NAMES,
            YARD_BRACKET_NAMES,
            _YARD_STATION_NAME_TO_KEY,
        )
    except Exception:
        return False
    name = _alarm_fence_name(alarm)
    if not name:
        return False
    if name in _AWASO_FENCE_NAMES:
        return True
    if name in YARD_WRAPPER_NAMES:
        return True
    if name in YARD_BRACKET_NAMES:
        return True
    if name in _YARD_STATION_NAME_TO_KEY:
        return True
    return False


def current_fence_state(imei: str,
                        now: datetime | None = None) -> dict:
    from jimi import fetch_alarms
    from report_analysis import _alarm_boundary_event, _alarm_time

    now = _utc(now) or datetime.now(timezone.utc)
    begin = now - timedelta(hours=_FENCE_LOOKBACK_HOURS)

    alarms = fetch_alarms(
        imei,
        begin.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    ) or []

    state = {
        "in_trip_time":   False,
        "in_awaso":       False,   # kept for API compat; now means "in yard"
        "last_event":     None,
        "lookback_hours": _FENCE_LOOKBACK_HOURS,
    }

    # Track the latest boundary event per fence group independently, so a
    # late-arriving or out-of-order alarm can't flip a flag after a newer
    # event has already set it. The comparison key is (timestamp, rank)
    # where rank prefers "in" over "out" at the same timestamp: a truck
    # that leaves one overlapping fence and enters another in the same
    # tick is inside the new one, so "in" must win regardless of list
    # order. Without the tiebreaker, events sharing a second (e.g.
    # "out LOADED AND PARKED" + "in MAINTENANCE TIME" at 15:33:09) would
    # leave in_awaso stuck at False depending on list order.
    last_trip_key: tuple | None = None
    last_yard_key: tuple | None = None
    last_at: datetime | None = None

    for a in alarms:
        ev = _alarm_boundary_event(a)
        if ev is None:
            continue
        t = _alarm_time(a)
        if t is None:
            continue

        is_trip = _fence_matches_trip_time(a)
        is_yard = _fence_matches_yard(a)
        if not (is_trip or is_yard):
            continue

        # Overall latest event across both fence groups.
        if last_at is None or t > last_at:
            last_at = t
            state["last_event"] = {
                "fence": "TRIP TIME" if is_trip else "YARD",
                "event": ev,
                "at":    t,
            }

        rank = 1 if ev == "in" else 0
        key = (t, rank)

        if is_trip and (last_trip_key is None or key > last_trip_key):
            last_trip_key = key
            state["in_trip_time"] = (ev == "in")
        if is_yard and (last_yard_key is None or key > last_yard_key):
            last_yard_key = key
            state["in_awaso"] = (ev == "in")

    return state


def _truck_has_open_trip(truck_id: str) -> dict | None:
    imei = _resolve_imei_for_truck(truck_id)
    if not imei:
        return None

    state = current_fence_state(imei)

    if state["in_awaso"] and not state["in_trip_time"]:
        return None

    last = state.get("last_event")

    if state["in_trip_time"]:
        return {
            "imei":   imei,
            "reason": "trip_time",
            "state":  state,
            "start":  last["at"] if last and last["fence"] == "TRIP TIME" else None,
        }

    if last is not None:
        return {
            "imei":   imei,
            "reason": "off_yard",
            "state":  state,
            "start":  None,
        }

    return {
        "imei":   imei,
        "reason": "stale",
        "state":  state,
        "start":  None,
    }


def describe_block(block: dict) -> str:
    reason = block.get("reason")
    if reason == "trip_time":
        return "Truck is on an open trip."
    if reason == "off_yard":
        return "Truck is not at the yard. Assign only if you know it has returned."
    if reason == "stale":
        return "Truck has not entered or left a tracked fence recently."
    return "Truck cannot be assigned right now."


def assign_driver(*, driver_id: str, truck_id: str,
                  assigned_at: datetime | None = None,
                  notes: str | None = None,
                  created_by: str | None = None,
                  override_active_trip: bool = False) -> dict:
    if not driver_id:
        raise DriverError("Missing driver id.")
    tid = _blank(truck_id)
    if not tid:
        raise DriverError("Missing truck name.")

    imei = _resolve_imei_for_truck(tid)
    if not imei:
        raise DriverError(
            f"Could not resolve a device for truck {tid!r}. "
            f"Make sure the truck name matches a device in Tracksolid."
        )

    driver = get_driver(driver_id)
    if not driver:
        raise DriverError("Driver not found.")
    if driver.get("deleted_at"):
        raise DriverError("Cannot assign a deleted driver.")

    when = _utc(assigned_at) or datetime.now(timezone.utc)

    existing_truck = current_assignment_for_imei(imei)
    if existing_truck and existing_truck["driver_id"] != driver_id:
        raise DriverError(
            f"Truck {existing_truck['truck_id'] or imei} is currently "
            f"assigned to another driver. Release it first."
        )
    existing_driver = current_assignment_for_driver(driver_id)
    if existing_driver and existing_driver["imei"] != imei:
        raise DriverError(
            f"{driver['full_name']} is currently assigned to "
            f"{existing_driver['truck_id'] or existing_driver['imei']}. "
            f"Release that assignment first."
        )

    if not override_active_trip:
        block = _truck_has_open_trip(tid)
        if block is not None:
            raise DriverError(describe_block(block))

    payload = {
        "driver_id":   driver_id,
        "imei":        imei,
        "truck_id":    tid,
        "assigned_at": when.isoformat(),
        "notes":       _blank(notes),
        "created_by":  created_by,
    }
    try:
        resp = (_client().table("truck_assignments")
                .insert(payload)
                .select(_ASSIGNMENT_SELECT)
                .execute())
    except Exception as exc:
        log.exception("assign_driver failed")
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc
    rows = resp.data or []
    if not rows:
        raise DriverError("Assignment was not created.")
    return _row_to_assignment(rows[0])


def release_assignment(assignment_id: str,
                       released_at: datetime | None = None,
                       reason: str = "manual") -> dict:
    if not assignment_id:
        raise DriverError("Missing assignment id.")
    when = _utc(released_at) or datetime.now(timezone.utc)
    try:
        resp = (_client().table("truck_assignments")
                .update({
                    "released_at": when.isoformat(),
                    "released_reason": reason,
                })
                .eq("id", assignment_id)
                .is_("released_at", "null")
                .select(_ASSIGNMENT_SELECT)
                .execute())
    except Exception as exc:
        log.exception("release_assignment failed for %s", assignment_id)
        raise DriverError(f"{type(exc).__name__}: {exc}") from exc
    rows = resp.data or []
    if not rows:
        raise DriverError("Assignment not found or already released.")
    return _row_to_assignment(rows[0])


def auto_release_finished_assignments(imei_trip_ends: Iterable[tuple[str, datetime]],
                                      reason: str = "trip_ended") -> int:
    by_imei: dict[str, datetime] = {}
    for imei, te in imei_trip_ends:
        if not imei or te is None:
            continue
        te = _utc(te)
        prev = by_imei.get(imei)
        if prev is None or te > prev:
            by_imei[imei] = te

    if not by_imei:
        return 0

    total = 0
    for imei, trip_end in by_imei.items():
        try:
            resp = (_client().table("truck_assignments")
                    .select("*")
                    .eq("imei", imei)
                    .lte("assigned_at", trip_end.isoformat())
                    .execute())
        except Exception as exc:
            log.warning("auto_release: select failed for %s: %s", imei, exc)
            continue

        for r in (resp.data or []):
            a = _row_to_assignment(r)
            rel = a["released_at"]
            if rel is not None and rel <= trip_end:
                continue
            try:
                (_client().table("truck_assignments")
                 .update({
                     "released_at": trip_end.isoformat(),
                     "released_reason": reason,
                 })
                 .eq("id", a["id"])
                 .execute())
                total += 1
            except Exception as exc:
                log.warning("auto_release: update failed for %s: %s",
                            a["id"], exc)

    return total


# ---------------------------------------------------------------------------
# Resolution — "who drove what, when" (unchanged)
# ---------------------------------------------------------------------------

def _windows_overlapping(rows: Iterable[dict],
                         begin: datetime, end: datetime) -> list[dict]:
    out: list[dict] = []
    for a in rows:
        start = a["assigned_at"]
        stop = a["released_at"]
        if start is None:
            continue
        if start > end:
            continue
        if stop is not None and stop < begin:
            continue
        out.append(a)
    return out


def imeis_for_driver_during(driver_id: str, begin: datetime,
                            end: datetime) -> list[dict]:
    if not driver_id:
        return []
    rows = list_assignments(driver_id=driver_id, limit=2000)
    rows = _windows_overlapping(rows, begin, end)
    by_imei: dict[str, dict] = {}
    for a in rows:
        key = a["imei"]
        entry = by_imei.setdefault(key, {
            "imei":     key,
            "truck_id": a["truck_id"],
            "windows":  [],
        })
        entry["windows"].append((a["assigned_at"], a["released_at"]))
    return list(by_imei.values())


def trucks_for_driver_during(driver_id: str, begin: datetime,
                             end: datetime) -> list[dict]:
    return imeis_for_driver_during(driver_id, begin, end)


def drivers_active_during(imei: str, begin: datetime,
                          end: datetime) -> list[dict]:
    if not imei:
        return []
    rows = list_assignments(imei=imei, limit=2000)
    rows = _windows_overlapping(rows, begin, end)
    driver_ids = list({a["driver_id"] for a in rows})
    names = {}
    if driver_ids:
        try:
            resp = (_client().table("drivers")
                    .select("id,full_name")
                    .in_("id", driver_ids).execute())
            for r in (resp.data or []):
                names[r["id"]] = r.get("full_name") or ""
        except Exception:
            log.exception("drivers_active_during: name lookup failed")

    out: dict[str, dict] = {}
    for a in rows:
        e = out.setdefault(a["driver_id"], {
            "driver_id": a["driver_id"],
            "full_name": names.get(a["driver_id"], ""),
            "windows":   [],
        })
        e["windows"].append((a["assigned_at"], a["released_at"]))
    return list(out.values())


def resolve_trip_driver(imei: str, trip_start: datetime) -> dict | None:
    if not imei or not trip_start:
        return None
    try:
        resp = (_client().table("truck_assignments").select("*")
                .eq("imei", imei)
                .lte("assigned_at", trip_start.isoformat())
                .execute())
    except Exception:
        log.exception("resolve_trip_driver: query failed for %s", imei)
        return None

    rows = [_row_to_assignment(r) for r in (resp.data or [])]
    if not rows:
        return None

    for a in rows:
        if a["released_at"] is not None and trip_start >= a["released_at"]:
            continue
        return _driver_brief(a["driver_id"])

    rows.sort(key=lambda a: a["assigned_at"] or datetime.min.replace(
        tzinfo=timezone.utc), reverse=True)
    return _driver_brief(rows[0]["driver_id"])


def _driver_brief(driver_id: str) -> dict | None:
    d = get_driver(driver_id)
    if not d:
        return None
    return {
        "driver_id": d["id"],
        "driver_name": d["full_name"],
    }


def annotate_trips_with_drivers(imei: str,
                                trips: list[dict]) -> list[dict]:
    if not trips:
        return trips
    try:
        resp = (_client().table("truck_assignments").select("*")
                .eq("imei", imei).execute())
    except Exception:
        log.exception("annotate_trips_with_drivers: fetch failed for %s", imei)
        return trips

    rows = [_row_to_assignment(r) for r in (resp.data or [])]
    if not rows:
        for t in trips:
            t.setdefault("driver_id", None)
            t.setdefault("driver_name", "")
        return trips

    names: dict[str, str] = {}
    driver_ids = list({r["driver_id"] for r in rows})
    try:
        r = (_client().table("drivers").select("id,full_name")
             .in_("id", driver_ids).execute())
        for row in (r.data or []):
            names[row["id"]] = row.get("full_name") or ""
    except Exception:
        log.exception("annotate_trips_with_drivers: name lookup failed")

    rows_sorted = sorted(rows, key=lambda a: a["assigned_at"] or
                        datetime.min.replace(tzinfo=timezone.utc))

    for t in trips:
        start = t.get("start")
        if not isinstance(start, datetime):
            t.setdefault("driver_id", None)
            t.setdefault("driver_name", "")
            continue
        chosen = None
        for a in rows_sorted:
            if a["assigned_at"] is None or a["assigned_at"] > start:
                continue
            if a["released_at"] is not None and start >= a["released_at"]:
                continue
            chosen = a
        if chosen is None:
            t["driver_id"] = None
            t["driver_name"] = ""
        else:
            t["driver_id"] = chosen["driver_id"]
            t["driver_name"] = names.get(chosen["driver_id"], "")

    return trips