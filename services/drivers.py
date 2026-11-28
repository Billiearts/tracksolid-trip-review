"""
services/drivers.py — driver registry, truck assignment, rest period,
and auto-release on trip end.

This module owns:
  * Driver CRUD (list/get/create/update/soft-delete).
  * Truck assignment (assign/release + history queries).
  * Rest period settings + rest_until() calculation.
  * Auto-release when a truck's trip ends (same fence-state logic the
    live map uses, so "trip ended" is consistent everywhere).

Semantics
---------
* Soft-deleting a driver (deleted_at) removes them from list views but
  leaves all their historic assignments intact. No new assignments can be
  made to a deleted driver.
* A truck currently on a trip cannot be reassigned without
  override_active_trip=True (admins / super-admins only).
* When a trip finishes (its end is known),
  `auto_release_finished_assignments` releases the driver from the truck
  with released_reason="trip_ended", so the next dispatch has to
  re-assign.
* Truck-state guard: a truck may only be assigned when its current fence
  state is inside the yard. The yard is not a single fence — it's a group
  (AWASO, the wrapper fences, and the station fences). A truck inside
  TRIP TIME is blocked as "on an open trip"; a truck outside both the
  yard and TRIP TIME is blocked as "not at the yard".
  See `current_fence_state()` below.

Fail-safe auto-release
----------------------
The auto-release path is *destructive*: if it fires when the truck is
actually still on a trip, it silently drops the driver's assignment.
So `_truck_has_open_trip()` treats "state lookup unavailable" as
"assume still on a trip" (a block), and `auto_release_if_trip_ended()`
short-circuits entirely when no fence-state helper exists on the
install. Both behaviours are required to stop the current bug where
every open assignment gets released on first call because
`current_fence_state()` returns None.

Rest period / Supabase client quirk
-----------------------------------
`_last_release_for_driver()` deliberately does NOT use
`.not_.is_("released_at", "null")`. On some versions of the Supabase
Python client that chain is a no-op and returns the open rows first
(ordered by released_at desc → the None row), so `rest_until()`
incorrectly returns None and the amber rest box never renders. We
instead fetch the 50 most recent rows ordered by `released_at desc`
and pick the first non-null value in Python.

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

Performance notes
-----------------
Driver searches used to be dramatically slower than truck searches
because `resolve_trip_driver()` was called once per trip and each call
did two Supabase round-trips (±24h assignment scan + driver-name
lookup). Two changes fixed that:

  * `resolve_trip_driver()` now does ONE query for the IMEI (all
    assignments, capped) and matches in Python, instead of a windowed
    query that could miss assignments made before the window.
  * A per-request cache (`_CACHE`) memoises assignment rows per IMEI
    and driver-name lookups. Callers should use
    `annotate_trips_with_drivers()` (single IMEI fetch for many trips)
    instead of looping `resolve_trip_driver()` per trip.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

from services.supabase_client import admin_client


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Allowed driver positions. Kept in sync with the CHECK constraint in
# sql/2026_11_23_driver_roles.sql.
DRIVER_ROLES = ("driver", "chief_driver", "trainee")
_ROLE_LABELS = {
    "driver":       "Driver",
    "chief_driver": "Chief driver",
    "trainee":      "Trainee",
}

# Singleton row id in driver_rest_settings.
REST_SETTINGS_ID = 1

# Fences used by the truck-state guard. TRIP TIME is matched by geoid
# (authoritative) with a name fallback; the yard is matched by name
# against the AWASO fences plus the wrapper/bracket/station fences from
# report_analysis.
_TRIP_TIME_FENCE_NAME_FALLBACKS = ("TRIP TIME GBC", "GBC TRIP TIME")
_AWASO_FENCE_NAMES = frozenset({"GBC AWASO", "GBC AWASO 2"})
_FENCE_LOOKBACK_HOURS = 72


# ---------------------------------------------------------------------------
# Per-request cache (see module docstring "Performance notes")
# ---------------------------------------------------------------------------

class _RequestCache:
    """Tiny per-call memo cache.

    Flask creates a fresh request context per request; we key off
    `flask.g` when available so the cache lives exactly as long as the
    request. Outside Flask (CLI / worker) the cache just lives for the
    duration of the call chain that created it.
    """

    _assignments: dict[str, list[dict]] = {}
    _driver_names: dict[str, str] = {}
    _assignment_selects: dict[tuple, list[dict]] = {}

    @classmethod
    def clear(cls) -> None:
        cls._assignments.clear()
        cls._driver_names.clear()
        cls._assignment_selects.clear()

    @classmethod
    def assignments_for_imei(cls, imei: str) -> list[dict] | None:
        return cls._assignments.get(imei)

    @classmethod
    def set_assignments_for_imei(cls, imei: str, rows: list[dict]) -> None:
        cls._assignments[imei] = rows

    @classmethod
    def driver_name(cls, driver_id: str) -> str | None:
        return cls._driver_names.get(driver_id)

    @classmethod
    def set_driver_name(cls, driver_id: str, name: str) -> None:
        cls._driver_names[driver_id] = name


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class DriverError(RuntimeError):
    """Raised for validation / lookup failures the UI can show."""


# ---------------------------------------------------------------------------
# Small helpers
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


def _parse_dt(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return _utc(v)
    try:
        return _utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Rest period settings + rest_until()
# ---------------------------------------------------------------------------

def get_rest_period_hours() -> float:
    """Return the configured rest period in hours (default 8)."""
    try:
        resp = (_client().table("driver_rest_settings")
                .select("rest_period_hours")
                .eq("id", REST_SETTINGS_ID)
                .limit(1)
                .execute())
        rows = resp.data or []
        if rows:
            return float(rows[0]["rest_period_hours"])
    except Exception:
        log.exception("get_rest_period_hours failed; defaulting to 8")
    return 8.0


def set_rest_period_hours(hours: float, updated_by: str | None = None) -> None:
    if hours < 0 or hours > 72:
        raise DriverError("Rest period must be between 0 and 72 hours.")
    payload = {
        "id": REST_SETTINGS_ID,
        "rest_period_hours": hours,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "updated_by": updated_by,
    }
    try:
        (_client().table("driver_rest_settings")
         .upsert(payload, on_conflict="id")
         .execute())
    except Exception as exc:
        log.exception("set_rest_period_hours failed")
        raise DriverError(f"Could not save rest period: {exc}") from exc


def _last_release_for_driver(driver_id: str) -> datetime | None:
    """Most recent released_at for this driver's assignments.

    Deliberately does NOT rely on Supabase's `.not_.is_()` chain — on
    some client versions the operator is a no-op and returns rows
    where released_at IS NULL, which makes rest_until() incorrectly
    return None. We fetch the last 50 rows ordered by released_at and
    pick the first non-null value in Python instead.
    """
    if not driver_id:
        return None
    try:
        resp = (_client().table("truck_assignments")
                .select("released_at")
                .eq("driver_id", driver_id)
                .order("released_at", desc=True)
                .limit(50)
                .execute())
    except Exception:
        log.exception("_last_release_for_driver failed for %s", driver_id)
        return None

    for row in (resp.data or []):
        raw = row.get("released_at")
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    return None


def rest_until(driver_id: str) -> datetime | None:
    """When the driver's rest period ends, or None if not resting.

    Rest is measured from the driver's most recent released assignment.
    A rest of 0 hours disables the feature entirely.
    """
    hours = get_rest_period_hours()
    if hours <= 0:
        return None
    last = _last_release_for_driver(driver_id)
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    ends = last + timedelta(hours=hours)
    if ends <= datetime.now(timezone.utc):
        return None
    return ends


# ---------------------------------------------------------------------------
# Row normalisers
# ---------------------------------------------------------------------------

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
# Driver CRUD
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
# Assignments — queries
# ---------------------------------------------------------------------------

_ASSIGNMENT_SELECT = (
    "id,driver_id,imei,truck_id,assigned_at,released_at,"
    "released_reason,notes,created_by,"
    "creator:created_by(email,full_name)"
)


def list_assignments(*, driver_id: str | None = None,
                     imei: str | None = None,
                     truck_id: str | None = None,
                     active_only: bool = False,
                     limit: int = 200) -> list[dict]:
    q = (_client().table("truck_assignments")
         .select(_ASSIGNMENT_SELECT))
    if driver_id:
        q = q.eq("driver_id", driver_id)
    if imei:
        q = q.eq("imei", imei)
    if truck_id:
        q = q.eq("truck_id", truck_id)
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


def current_assignment_for_truck(truck_id: str) -> dict | None:
    if not truck_id:
        return None
    rows = list_assignments(truck_id=truck_id, active_only=True, limit=1)
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Truck-state guard — fence-based
# ---------------------------------------------------------------------------

def _truck_state_available() -> bool:
    """True if a real fence-state helper exists and can be called."""
    try:
        from services import devices as devices_svc
        return getattr(devices_svc, "current_fence_state", None) is not None
    except Exception:
        return False


def current_fence_state(truck_id: str) -> dict | None:
    """Return the truck's current fence state, or None if unavailable.

    Delegates to the devices service helper when present. Returns None
    (rather than raising) when the helper is missing or errors, so
    callers can distinguish "state unknown" from "confirmed not on
    trip" — the auto-release path relies on that distinction.
    """
    try:
        from services import devices as devices_svc
        fn = getattr(devices_svc, "current_fence_state", None)
        if fn is not None:
            return fn(truck_id)
    except Exception:
        log.exception("current_fence_state failed for %s", truck_id)
    return None


def _fence_matches_trip_time(alarm: dict) -> bool:
    """Kept for parity with the local-fence implementation. Only used
    when the devices-service helper is missing (see
    `_local_fence_state`).
    """
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


def _local_fence_state(imei: str,
                       now: datetime | None = None) -> dict:
    """Local fence-state computation, used only when the devices
    service does not expose `current_fence_state`. Matches the
    {'fence_kind': 'trip_time'|'yard', 'inside': bool, 'since': dt}
    shape the devices helper returns.
    """
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
        "fence_kind":      None,
        "inside":          False,
        "since":           None,
        "in_trip_time":    False,
        "in_awaso":        False,
        "last_event":      None,
        "lookback_hours":  _FENCE_LOOKBACK_HOURS,
    }

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
            if ev == "in":
                state["fence_kind"] = "trip_time"
                state["inside"] = True
                state["since"] = t
        if is_yard and (last_yard_key is None or key > last_yard_key):
            last_yard_key = key
            state["in_awaso"] = (ev == "in")

    return state


def _truck_has_open_trip(truck_id: str) -> dict | None:
    """Return a block dict iff the truck is in TRIP TIME.

    Fails SAFE: if no fence-state helper is available on this install,
    return a block dict so `auto_release_if_trip_ended` does NOT fire.
    The block dict distinguishes 'no state' (reason='state_unavailable')
    from 'confirmed on trip' (reason='trip_time').
    """
    if not _truck_state_available():
        return {"reason": "state_unavailable", "start": None,
                "state": None}
    state = current_fence_state(truck_id)
    if not state:
        return {"reason": "state_unavailable", "start": None,
                "state": None}
    if state.get("fence_kind") == "trip_time" and state.get("inside"):
        return {
            "reason": "trip_time",
            "start":  state.get("since"),
            "state":  state,
        }
    return None


def describe_block(block: dict) -> str:
    reason = block.get("reason")
    if reason == "trip_time":
        return "Truck is on an open trip."
    if reason == "off_yard":
        return "Truck is not at the yard. Assign only if you know it has returned."
    if reason == "stale":
        return "Truck has not entered or left a tracked fence recently."
    if reason == "state_unavailable":
        return ("Truck state is unavailable on this install; "
                "refusing to auto-release.")
    return "Truck cannot be assigned right now."


# ---------------------------------------------------------------------------
# IMEI resolution
# ---------------------------------------------------------------------------

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


def _resolve_imei_fallback(truck_id: str) -> str:
    """Fallback path used only by assign_driver when find_device is
    unavailable. Returns '' (not None) to match the DB column default.
    """
    try:
        from services.devices import get_devices_cached_only, get_devices
        devices = None
        try:
            devices = get_devices_cached_only()
        except Exception:
            devices = None
        if devices is None:
            devices = get_devices()
        for d in devices or []:
            if (d.get("deviceName") or "").strip() == truck_id:
                return d.get("imei") or ""
    except Exception:
        log.exception("_resolve_imei_fallback failed for %s", truck_id)
    return ""


# ---------------------------------------------------------------------------
# Assignment — write paths
# ---------------------------------------------------------------------------

def assign_driver(driver_id, truck_id, notes, created_by,
                  override_rest=False):
    """Create a new assignment row.

    Rules:
      * The driver must have no open assignment.
      * The truck must have no open assignment (can't hand an already
        assigned truck to a different driver).
      * The driver must not still be in their rest period
        (unless override_rest).

    `created_by` / `assigned_by` are only written when we actually have
    a non-empty string. Both columns have FK constraints (to profiles /
    auth.users), so passing None or a malformed id would abort the
    insert.
    """
    client = _client()
    # 1. Driver already assigned?
    cur = current_assignment_for_driver(driver_id)
    if cur is not None:
        raise DriverError(
            f"Driver is already assigned to {cur['truck_id'] or cur['imei']}."
        )

    # 2. Truck already assigned to someone else?
    try:
        resp = (client.table("truck_assignments")
                .select("id, driver_id, truck_id, imei")
                .eq("truck_id", truck_id)
                .is_("released_at", "null")
                .limit(1)
                .execute())
        rows = resp.data or []
    except Exception as exc:
        log.exception("assign_driver: truck assignment lookup failed")
        raise DriverError(
            f"Could not verify the truck's assignment state: {exc}"
        ) from exc
    if rows:
        raise DriverError(
            f"{truck_id} is already assigned to another driver."
        )

    # 3. Rest period
    if not override_rest:
        ru = rest_until(driver_id)
        if ru is not None:
            raise DriverError(
                f"Driver is resting until "
                f"{ru.strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Rest period is {get_rest_period_hours():g}h after release."
            )

    # 4. Resolve IMEI server-side (never trust the browser)
    imei = _resolve_imei_for_truck(truck_id) or _resolve_imei_fallback(truck_id)

    # 5. Build payload. Only include created_by / assigned_by when we
    #    actually have a non-empty string, otherwise omit them so the
    #    FKs to profiles(id) / auth.users(id) don't block the insert.
    payload = {
        "driver_id": driver_id,
        "truck_id":  truck_id,
        "imei":      imei,
        "notes":     notes,
    }
    actor = created_by.strip() if isinstance(created_by, str) else None
    if actor:
        payload["created_by"] = actor
        payload["assigned_by"] = actor

    try:
        resp = client.table("truck_assignments").insert(payload).execute()
    except Exception as exc:
        log.exception("assign_driver: insert failed payload=%s", payload)
        raise DriverError(f"Could not create assignment: {exc}") from exc
    rows = resp.data or []
    if not rows:
        raise DriverError("Assignment was not created.")

    # Invalidate any cached assignment list for this IMEI so the next
    # resolve/annotate call sees the new row.
    _RequestCache._assignments.pop(imei, None)
    return rows[0]


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

    # Any release changes the assignment timeline for its IMEI.
    released = _row_to_assignment(rows[0])
    _RequestCache._assignments.pop(released["imei"], None)
    return released


# ---------------------------------------------------------------------------
# Auto-release on trip end
# ---------------------------------------------------------------------------

def auto_release_if_trip_ended(truck_id: str) -> dict | None:
    """Release an open assignment ONLY when we can confirm the truck
    is not on a trip AND a fence-state helper is available.

    If fence-state is unavailable, do nothing (fail-safe).
    """
    if not truck_id:
        return None
    if not _truck_state_available():
        # No state lookup on this install → don't touch the row.
        return None

    try:
        resp = (_client().table("truck_assignments")
                .select("*")
                .eq("truck_id", truck_id)
                .is_("released_at", "null")
                .limit(1)
                .execute())
    except Exception:
        log.exception("auto_release_if_trip_ended: lookup failed for %s",
                      truck_id)
        return None
    rows = resp.data or []
    if not rows:
        return None
    assignment = rows[0]

    block = _truck_has_open_trip(truck_id)
    if block is not None:
        # Still on a trip (or state unavailable) → leave it alone.
        return None

    try:
        return release_assignment(assignment["id"], reason="trip_ended")
    except DriverError:
        raise
    except Exception:
        log.exception("auto_release_if_trip_ended: release failed for %s",
                      truck_id)
        return None


def auto_release_finished_assignments(
        imei_trip_ends: Iterable[tuple[str, datetime]],
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

        # Timeline for this IMEI just changed.
        _RequestCache._assignments.pop(imei, None)

    return total


# ---------------------------------------------------------------------------
# Resolution — "who drove what, when"
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


def _fetch_assignments_for_imei(imei: str,
                                limit: int = 2000) -> list[dict]:
    """Fetch all assignments for an IMEI, memoised per request.

    This is the single network call used by both `resolve_trip_driver`
    and `annotate_trips_with_drivers`. Caching it means a driver search
    that resolves N trips pays 1 query, not N.
    """
    if not imei:
        return []
    cached = _RequestCache.assignments_for_imei(imei)
    if cached is not None:
        return cached

    try:
        resp = (_client().table("truck_assignments")
                .select(_ASSIGNMENT_SELECT)
                .eq("imei", imei)
                .order("assigned_at", desc=False)
                .limit(max(1, min(2000, limit)))
                .execute())
    except Exception:
        log.exception("_fetch_assignments_for_imei failed for %s", imei)
        return []

    rows = [_row_to_assignment(r) for r in (resp.data or [])]
    _RequestCache.set_assignments_for_imei(imei, rows)
    return rows


def _driver_name_cached(driver_id: str) -> str:
    """Driver-name lookup with a per-request cache.

    Avoids re-hitting the drivers table once per trip in a search.
    """
    if not driver_id:
        return ""
    cached = _RequestCache.driver_name(driver_id)
    if cached is not None:
        return cached
    name = ""
    try:
        resp = (_client().table("drivers")
                .select("full_name")
                .eq("id", driver_id)
                .limit(1)
                .execute())
        rows = resp.data or []
        if rows:
            name = rows[0].get("full_name") or ""
    except Exception:
        log.exception("_driver_name_cached failed for %s", driver_id)
    _RequestCache.set_driver_name(driver_id, name)
    return name


def trucks_for_driver_during(driver_id: str, begin: datetime,
                             end: datetime) -> list[dict]:
    if not driver_id:
        return []

    rows = list_assignments(driver_id=driver_id, limit=2000)

    # We intentionally do NOT filter by [begin, end] here. We return all
    # assignments so the blueprint can check if the driver held the truck
    # at ANY point during the trip (including mid-trip assignments).
    by_imei: dict[str, dict] = {}
    for a in rows:
        if a["assigned_at"] is None:
            continue
        key = a["imei"]
        entry = by_imei.setdefault(key, {
            "imei":     key,
            "truck_id": a["truck_id"],
            "windows":  [],
        })
        entry["windows"].append((a["assigned_at"], a["released_at"]))

    return list(by_imei.values())


def drivers_active_during(imei: str, begin: datetime,
                          end: datetime) -> list[dict]:
    if not imei:
        return []
    rows = _fetch_assignments_for_imei(imei)
    rows = _windows_overlapping(rows, begin, end)
    driver_ids = list({a["driver_id"] for a in rows})

    out: dict[str, dict] = {}
    for a in rows:
        e = out.setdefault(a["driver_id"], {
            "driver_id": a["driver_id"],
            "full_name": _driver_name_cached(a["driver_id"]),
            "windows":   [],
        })
        e["windows"].append((a["assigned_at"], a["released_at"]))
    return list(out.values())


def resolve_trip_driver(imei: str, trip_start: datetime) -> dict | None:
    """Resolve the driver for a trip by looking at the assignment window.

    Matching strategy (first hit wins):
      1. Exact window match: assigned_at <= trip_start < released_at
         (or released_at is NULL).
      2. Mid-trip fallback: the earliest assignment that *starts after*
         trip_start (driver was assigned during the trip).
      3. Nearest fallback: the assignment whose assigned_at is closest
         to trip_start (handles late dispatches / clock skew).

    All assignments for the IMEI are fetched with ONE query and
    memoised per request, so calling this in a loop over N trips
    costs one network round-trip total (plus driver-name lookups,
    which are also cached).
    """
    if not imei or trip_start is None:
        return None

    trip_start = _utc(trip_start)
    rows = _fetch_assignments_for_imei(imei)
    if not rows:
        return None

    # Sort defensively in case the DB returned unsorted rows.
    rows_sorted = sorted(
        rows,
        key=lambda a: a["assigned_at"] or
        datetime.min.replace(tzinfo=timezone.utc),
    )

    # 1. Exact window match.
    for a in rows_sorted:
        start = a["assigned_at"]
        if start is None or start > trip_start:
            continue
        stop = a["released_at"]
        if stop is not None and stop <= trip_start:
            continue
        return _driver_brief_cached(a["driver_id"])

    # 2. Mid-trip fallback: first assignment strictly after trip_start.
    for a in rows_sorted:
        start = a["assigned_at"]
        if start is not None and start > trip_start:
            return _driver_brief_cached(a["driver_id"])

    # 3. Nearest fallback.
    nearest = min(
        rows_sorted,
        key=lambda a: abs((a["assigned_at"] - trip_start).total_seconds())
        if a["assigned_at"] is not None else float("inf"),
    )
    if nearest["driver_id"]:
        return _driver_brief_cached(nearest["driver_id"])

    return None


def _driver_brief_cached(driver_id: str) -> dict | None:
    """Cached `{driver_id, driver_name}` payload for resolve_trip_driver."""
    if not driver_id:
        return None
    name = _driver_name_cached(driver_id)
    if not name:
        return None
    return {"driver_id": driver_id, "driver_name": name}


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
    """Attach `driver_id` / `driver_name` to each trip in `trips`.

    Uses a single assignment fetch per IMEI plus the cached driver-name
    lookup, so annotating M trips for one truck costs one network call
    for assignments and at most K for names (K = distinct drivers).
    This is the preferred path for driver-search report generation —
    it replaces a per-trip `resolve_trip_driver()` loop.
    """
    if not trips:
        return trips

    rows = _fetch_assignments_for_imei(imei)
    if not rows:
        for t in trips:
            t.setdefault("driver_id", None)
            t.setdefault("driver_name", "")
        return trips

    rows_sorted = sorted(
        rows,
        key=lambda a: a["assigned_at"] or
        datetime.min.replace(tzinfo=timezone.utc),
    )

    for t in trips:
        start = t.get("start")
        if not isinstance(start, datetime):
            t.setdefault("driver_id", None)
            t.setdefault("driver_name", "")
            continue

        start = _utc(start)
        chosen = None

        # 1. Exact window match.
        for a in rows_sorted:
            a_start = a["assigned_at"]
            if a_start is None or a_start > start:
                continue
            a_stop = a["released_at"]
            if a_stop is not None and a_stop <= start:
                continue
            chosen = a
            break

        # 2. Mid-trip fallback.
        if chosen is None:
            for a in rows_sorted:
                if a["assigned_at"] is not None and a["assigned_at"] > start:
                    chosen = a
                    break

        # 3. Nearest fallback.
        if chosen is None:
            chosen = min(
                rows_sorted,
                key=lambda a: abs(
                    (a["assigned_at"] - start).total_seconds()
                ) if a["assigned_at"] is not None else float("inf"),
            )

        if chosen is None or not chosen["driver_id"]:
            t["driver_id"] = None
            t["driver_name"] = ""
        else:
            t["driver_id"] = chosen["driver_id"]
            t["driver_name"] = _driver_name_cached(chosen["driver_id"])

    return trips