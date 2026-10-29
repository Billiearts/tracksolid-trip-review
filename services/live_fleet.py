"""
services/live_fleet.py — Live Awaso↔Takoradi corridor view.

Yard fences (WAITING TIME, FUELING TIME, LOADING TIME):
  Alarm events from Tracksolid are authoritative *for dwell history*.
  For the live "is this truck inside right now" count we combine alarms
  with GPS (see _resolve_yard_membership).

Corridor facilities (Awaso / Takoradi):
  Loaded from fence_polygons where kind IN ('awaso','takoradi').
  Both shapes are supported (polygon-first, circle fallback).

Scanning control (GLOBAL, admin-only):
  A single process-wide `_paused` flag. When False (default), any visitor
  — admin or not — triggers a live scan on demand (subject to the usual
  10-second cache). When True, get_fleet_state() returns the cached
  snapshot without calling Tracksolid, and *everyone* sees the same
  frozen frame. Only admins can flip the flag via /live/api/scan.

Diagnostics:
  get_fleet_state also returns a `diagnostics` dict that the API passes
  through to the front end.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4

from jimi import api_call
from services.devices import classify_device, get_devices
from services.supabase_client import admin_client


log = logging.getLogger(__name__)

CORRIDOR_LOOKBACK_DAYS = 7
MOVING_KPH             = 5.0
CACHE_TTL_SEC          = 10
OFFLINE_MIN            = 60

_LOCATION_BATCH_SIZE   = 10
_CORRIDOR_STATE_TTL    = timedelta(minutes=15)
_FACILITIES_TTL_SEC    = 30

_YARD_ALARM_LOOKBACK_HOURS = 24
_YARD_ALARM_PAGE_SIZE      = 500
_YARD_STATE_TTL_SEC        = 60
_YARD_STATE_MAX_AGE        = timedelta(hours=6)
_YARD_GPS_TRUST_AGE        = timedelta(minutes=15)

_YARD_FENCES = (
    {"key": "waiting", "fence_name": "WAITING TIME", "label": "Dispatch"},
    {"key": "fueling", "fence_name": "FUELING TIME", "label": "Workshop"},
    {"key": "loading", "fence_name": "LOADING TIME", "label": "Washing Plant"},
)

_YARD_KEY_BY_NAME = {f["fence_name"].upper(): f["key"] for f in _YARD_FENCES}

_CORRIDOR_KEYS = ("awaso", "takoradi")
_CORRIDOR_LABELS = {"awaso": "Awaso", "takoradi": "Takoradi"}

_CORRIDOR_DEFAULTS = {
    "awaso":    {"lat": 6.2298,   "lng": -2.284723, "radius_km": 2.15},
    "takoradi": {"lat": 4.893897, "lng": -1.748176, "radius_km": 0.40},
}


# ---------------------------------------------------------------------------
# Scan pause (GLOBAL, admin-only)
# ---------------------------------------------------------------------------
# A single process-wide flag. False = live scanning allowed for everyone.
# True = no Tracksolid calls at all; every caller (admin or not) is served
# the last cached snapshot. Only /live/api/scan (admin) toggles it.
#
# The session_id argument is kept for API compatibility with older callers
# but is ignored — pausing is no longer per-session.

_paused: bool = False
_paused_since: datetime | None = None
_paused_lock = Lock()


def is_scanning(session_id: str | None = None, default: bool = True) -> bool:
    """Return True if live scanning is allowed right now (i.e. not paused).

    `session_id` and `default` are ignored. Kept so existing callers
    (blueprint, anything else importing this) keep working unchanged.
    """
    with _paused_lock:
        return not _paused


def set_scanning(session_id: str | None, enabled: bool) -> None:
    """Set the GLOBAL pause flag.

    `enabled=True`  -> resume live scanning.
    `enabled=False` -> pause; everyone sees the last cached snapshot.

    `session_id` is ignored (kept for API compatibility).
    """
    global _paused, _paused_since
    with _paused_lock:
        _paused = not bool(enabled)
        if _paused:
            if _paused_since is None:
                _paused_since = datetime.now(timezone.utc)
        else:
            _paused_since = None


def paused_since() -> datetime | None:
    """When the current pause began, or None if not paused."""
    with _paused_lock:
        return _paused_since


def forget_session(session_id: str | None) -> None:
    """No-op retained for compatibility (pause is no longer per-session)."""
    return


# ---------------------------------------------------------------------------
# Alarm helpers
# ---------------------------------------------------------------------------

_FENCE_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")


def _alarm_fence_name(alarm: dict) -> str:
    name = (alarm.get("alarmTypeName") or "").strip()
    m = _FENCE_SUFFIX_RE.search(name)
    return m.group(1).strip() if m else ""


def _alarm_event(alarm: dict) -> str | None:
    eid = (alarm.get("alertTypeId") or "").strip().lower()
    return eid if eid in ("in", "out") else None


def _alarm_time(alarm: dict) -> datetime | None:
    ts = alarm.get("alertTime")
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0088
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _polygon_centroid(points: list[list[float]]) -> tuple[float, float]:
    n = len(points)
    return (
        sum(p[0] for p in points) / n,
        sum(p[1] for p in points) / n,
    )


def _polygon_bounding_radius_km(points: list[list[float]]) -> float:
    clat, clng = _polygon_centroid(points)
    return max(_haversine_km(clat, clng, p[0], p[1]) for p in points)


def _polygon_area_km2(points: list[list[float]]) -> float:
    if len(points) < 3:
        return 0.0
    clat, _ = _polygon_centroid(points)
    kx = 111.32 * math.cos(math.radians(clat))
    ky = 111.32
    area = 0.0
    n = len(points)
    for i in range(n):
        y1, x1 = points[i]
        y2, x2 = points[(i + 1) % n]
        area += (x1 * kx) * (y2 * ky) - (x2 * kx) * (y1 * ky)
    return abs(area) / 2.0


def _point_in_polygon(lat: float, lng: float, points: list[list[float]]) -> bool:
    inside = False
    n = len(points)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = points[i]
        yj, xj = points[j]
        if ((yi > lat) != (yj > lat)) and \
           (lng < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _validate_polygon(points) -> list[list[float]] | None:
    if not isinstance(points, list) or len(points) < 3:
        return None
    out: list[list[float]] = []
    for p in points:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None
        try:
            la = float(p[0]); lo = float(p[1])
        except (TypeError, ValueError):
            return None
        if not (-90 <= la <= 90) or not (-180 <= lo <= 180):
            return None
        out.append([la, lo])
    if len(out) >= 4 and out[0] == out[-1]:
        out = out[:-1]
    if len(out) < 3:
        return None
    if _polygon_area_km2(out) <= 0.0:
        return None
    return out


# ---------------------------------------------------------------------------
# Facility loading
# ---------------------------------------------------------------------------

_facilities_lock = Lock()
_facilities_cache: dict = {"data": None, "expires_at": 0.0}


def _row_to_facility(row: dict) -> dict | None:
    name = (row.get("name") or "").strip()
    kind = (row.get("kind") or "").strip().lower()
    if not name and not kind:
        return None

    shape = (row.get("shape") or "circle").lower()

    polygon = None
    if shape == "polygon" and isinstance(row.get("polygon"), list):
        polygon = _validate_polygon(row["polygon"])

    try:
        lat = float(row["lat"])
        lng = float(row["lng"])
        radius_km = float(row["radius_km"])
    except (TypeError, ValueError, KeyError):
        return None

    if polygon is not None:
        clat, clng = _polygon_centroid(polygon)
        lat, lng = clat, clng
        radius_km = _polygon_bounding_radius_km(polygon)
        shape = "polygon"
    else:
        shape = "circle"
        polygon = None

    if kind in _CORRIDOR_KEYS:
        family = "corridor"
        key = kind
        label = _CORRIDOR_LABELS.get(kind, name or kind.title())
    elif kind in ("yard", "custom"):
        family = "yard"
        key = _YARD_KEY_BY_NAME.get(name.upper())
        if key is not None:
            label = next(
                (s["label"] for s in _YARD_FENCES if s["key"] == key),
                name,
            )
        else:
            label = name
    else:
        family = "custom"
        key = None
        label = name

    return {
        "id":         row.get("id"),
        "geoid":      row.get("geoid"),
        "kind":       kind or "custom",
        "family":     family,
        "key":        key,
        "name":       name,
        "fence_name": name,
        "label":      label,
        "shape":      shape,
        "lat":        lat,
        "lng":        lng,
        "radius_km":  radius_km,
        "polygon":    polygon,
    }


_row_to_yard = _row_to_facility


def _load_facilities_diagnostic() -> tuple[list[dict], list[dict], str | None]:
    now = time.time()
    with _facilities_lock:
        if (_facilities_cache["data"] is not None
                and now < _facilities_cache["expires_at"]):
            corridor, yards = _facilities_cache["data"]
            return corridor, yards, _facilities_cache.get("error")

    corridor: list[dict] = []
    yards: list[dict] = []
    error: str | None = None

    try:
        resp = admin_client().table("fence_polygons").select("*").execute()
        rows = resp.data or []
        log.info("live_fleet: fence_polygons returned %d row(s)", len(rows))
        for r in rows:
            fac = _row_to_facility(r)
            if fac is None:
                log.warning("live_fleet: skipping malformed fence row: %r", r)
                continue
            if fac["family"] == "corridor":
                corridor.append(fac)
            else:
                yards.append(fac)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        log.warning("live_fleet: fence_polygons load failed: %s", error)

    seen_keys = {y["key"] for y in yards if y.get("key")}
    for spec in _YARD_FENCES:
        if spec["key"] not in seen_keys:
            yards.append({
                "id":         None,
                "geoid":      None,
                "kind":       "yard",
                "family":     "yard",
                "key":        spec["key"],
                "name":       spec["fence_name"],
                "fence_name": spec["fence_name"],
                "label":      spec["label"],
                "shape":      "none",
                "lat":        None,
                "lng":        None,
                "radius_km":  None,
                "polygon":    None,
            })

    seen_corridor = {c["kind"] for c in corridor}
    for kind in _CORRIDOR_KEYS:
        if kind not in seen_corridor:
            corridor.append({
                "id":         None,
                "geoid":      None,
                "kind":       kind,
                "family":     "corridor",
                "key":        kind,
                "name":       _CORRIDOR_LABELS[kind],
                "fence_name": _CORRIDOR_LABELS[kind],
                "label":      _CORRIDOR_LABELS[kind],
                "shape":      "none",
                "lat":        None,
                "lng":        None,
                "radius_km":  None,
                "polygon":    None,
            })

    payload = (corridor, yards)
    with _facilities_lock:
        _facilities_cache["data"] = payload
        _facilities_cache["error"] = error
        _facilities_cache["expires_at"] = now + _FACILITIES_TTL_SEC

    log.info(
        "live_fleet: facilities loaded — corridor: %d | yards: %d | error=%r",
        len(corridor), len(yards), error,
    )
    return corridor, yards, error


def _load_facilities() -> tuple[list[dict], list[dict]]:
    corridor, yards, _err = _load_facilities_diagnostic()
    return corridor, yards


def invalidate_facilities_cache() -> None:
    with _facilities_lock:
        _facilities_cache["expires_at"] = 0.0


def seed_default_fences() -> dict:
    client = admin_client()
    existing = client.table("fence_polygons").select("kind,name").execute()
    rows = existing.data or []
    have_kinds = {(r.get("kind") or "").lower() for r in rows}
    have_yard_names = {
        (r.get("name") or "").upper()
        for r in rows
        if (r.get("kind") or "").lower() in ("yard", "custom")
    }

    inserted = 0
    skipped = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    def _insert(record: dict) -> None:
        nonlocal inserted
        record.setdefault("created_at", now_iso)
        record["updated_at"] = now_iso
        client.table("fence_polygons").insert(record).execute()
        inserted += 1

    for kind in _CORRIDOR_KEYS:
        if kind in have_kinds:
            skipped += 1
            continue
        defaults = _CORRIDOR_DEFAULTS[kind]
        _insert({
            "name":       _CORRIDOR_LABELS[kind],
            "kind":       kind,
            "shape":      "circle",
            "lat":        defaults["lat"],
            "lng":        defaults["lng"],
            "radius_km":  defaults["radius_km"],
            "polygon":    None,
        })

    for spec in _YARD_FENCES:
        if spec["fence_name"].upper() in have_yard_names:
            skipped += 1
            continue
        _insert({
            "name":       spec["fence_name"],
            "kind":       "yard",
            "shape":      "circle",
            "lat":        _CORRIDOR_DEFAULTS["awaso"]["lat"],
            "lng":        _CORRIDOR_DEFAULTS["awaso"]["lng"],
            "radius_km":  0.2,
            "polygon":    None,
        })

    invalidate_facilities_cache()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Inside tests
# ---------------------------------------------------------------------------

def _inside_facility(lat, lng, facilities):
    best = None
    best_area = float("inf")
    for f in facilities:
        poly = f.get("polygon")
        if f.get("shape") == "polygon" and poly:
            if _point_in_polygon(lat, lng, poly):
                a = _polygon_area_km2(poly)
                if a < best_area:
                    best_area = a
                    best = f
    if best is not None:
        return best
    for f in facilities:
        if f.get("shape") == "circle" and f.get("radius_km") is not None:
            d = _haversine_km(lat, lng, f["lat"], f["lng"])
            if d <= f["radius_km"]:
                a = math.pi * (f["radius_km"] ** 2)
                if a < best_area:
                    best_area = a
                    best = f
    return best


def _inside_yard(lat, lng, yards):
    best = None
    best_area = float("inf")
    for y in yards:
        poly = y.get("polygon")
        if y.get("shape") == "polygon" and poly:
            if _point_in_polygon(lat, lng, poly):
                a = _polygon_area_km2(poly)
                if a < best_area:
                    best_area = a
                    best = y
    if best is not None:
        return best
    for y in yards:
        if y.get("shape") == "circle" and y.get("radius_km") is not None:
            d = _haversine_km(lat, lng, y["lat"], y["lng"])
            if d <= y["radius_km"]:
                a = math.pi * (y["radius_km"] ** 2)
                if a < best_area:
                    best_area = a
                    best = y
    return best


_inside_yard_gps = _inside_yard


# ---------------------------------------------------------------------------
# Corridor event state
# ---------------------------------------------------------------------------

def _derive_event_state(imei: str, lookback_days: int = CORRIDOR_LOOKBACK_DAYS) -> str | None:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=lookback_days)
    try:
        r = api_call("jimi.device.alarm.list", {
            "imeis":      imei,
            "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
            "page": 1, "page_size": 500,
        })
    except Exception as exc:
        log.warning("alarm.list(%s) failed: %s", imei, exc)
        return None

    result = r.get("result") or r.get("data") or []
    if isinstance(result, dict):
        result = result.get("list") or []
    if not result:
        return None

    result.sort(key=lambda a: a.get("alertTime") or "", reverse=True)

    for a in result:
        name = _alarm_fence_name(a)
        event = (a.get("alertTypeId") or "").lower()
        if "TRIP TIME" in name:
            continue
        if name in ("GBC EXITED", "GBC EXIT", "HAULAGE TRUCKS EXIT") and event == "out":
            return "heading_to_takoradi"
        if name in ("TAKORADI ENTRY", "GBC TAKORADI", "GBC TAKORADI 2") and event == "out":
            return "heading_to_awaso"
    return None


# ---------------------------------------------------------------------------
# Yard-fence state from alarms
# ---------------------------------------------------------------------------

_yard_state_cache: dict[str, dict] = {}
_yard_state_lock = Lock()


def _fetch_recent_yard_alarms(imeis: list[str]) -> dict[str, list[dict]]:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(hours=_YARD_ALARM_LOOKBACK_HOURS)
    out: dict[str, list[dict]] = {imei: [] for imei in imeis}
    BATCH = 10
    for i in range(0, len(imeis), BATCH):
        chunk = imeis[i:i + BATCH]
        try:
            r = api_call("jimi.device.alarm.list", {
                "imeis":      ",".join(chunk),
                "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
                "page": 1, "page_size": _YARD_ALARM_PAGE_SIZE,
            })
            result = r.get("result") or r.get("data") or []
            if isinstance(result, dict):
                result = result.get("list") or []
            for a in result:
                imei = (a.get("imei") or "").strip()
                if imei in out:
                    out[imei].append(a)
        except Exception as exc:
            log.warning("yard-alarm batch failed: %s", exc)
    return out


def _derive_yard_state_from_alarms(alarms: list[dict]) -> dict | None:
    if not alarms:
        return None
    alarms = sorted(alarms, key=lambda a: a.get("alertTime") or "", reverse=True)
    decided: dict[str, str] = {}
    now = datetime.now(timezone.utc)

    for a in alarms:
        if len(decided) == len(_YARD_FENCES):
            break
        name = _alarm_fence_name(a).upper()
        key = _YARD_KEY_BY_NAME.get(name)
        if key is None or key in decided:
            continue
        ev = _alarm_event(a)
        if ev is None:
            continue
        decided[key] = ev
        if ev == "in":
            t = _alarm_time(a)
            if t is not None and (now - t) > _YARD_STATE_MAX_AGE:
                log.debug(
                    "yard-state: ignoring stale 'in' for %s (%s) at %s",
                    key, name, t,
                )
                continue
            spec = next(s for s in _YARD_FENCES if s["key"] == key)
            return {
                "key":       key,
                "label":     spec["label"],
                "name":      spec["fence_name"],
                "radius_km": None,
                "source":    "alarm",
                "since":     t.isoformat() if t else None,
            }
    return None


def _refresh_yard_state(imeis: list[str], yard_facilities: list[dict]) -> dict[str, dict | None]:
    now = time.time()
    result: dict[str, dict | None] = {}
    with _yard_state_lock:
        stale: list[str] = []
        for imei in imeis:
            entry = _yard_state_cache.get(imei)
            if entry is not None and now < entry["expires_at"]:
                result[imei] = entry["state"]
            else:
                stale.append(imei)
    if not stale:
        return result
    alarms_by_imei = _fetch_recent_yard_alarms(stale)
    with _yard_state_lock:
        for imei in stale:
            payload = _derive_yard_state_from_alarms(alarms_by_imei.get(imei) or [])
            _yard_state_cache[imei] = {
                "state": payload,
                "expires_at": now + _YARD_STATE_TTL_SEC,
            }
            result[imei] = payload
    return result


# ---------------------------------------------------------------------------
# Motion + track location helpers
# ---------------------------------------------------------------------------

def _motion_state(speed_kph, gps_time, now):
    if gps_time is None:
        return "offline"
    age_min = (now - gps_time).total_seconds() / 60.0
    if age_min > OFFLINE_MIN:
        return "offline"
    if speed_kph >= MOVING_KPH:
        return "moving"
    return "parked" if age_min > 10 else "idle"


def _fetch_locations_batched(imeis: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(imeis), _LOCATION_BATCH_SIZE):
        chunk = imeis[i:i + _LOCATION_BATCH_SIZE]
        if not chunk:
            continue
        try:
            r = api_call("jimi.device.location.get", {"imeis": ",".join(chunk)})
            result = r.get("result") or r.get("data") or []
            if isinstance(result, dict):
                result = result.get("list") or []
            for loc in result:
                if loc.get("imei"):
                    out[loc["imei"]] = loc
        except Exception as exc:
            log.warning("location.get chunk failed: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Corridor cache
# ---------------------------------------------------------------------------

_corridor_cache: dict[str, dict] = {}
_corridor_cache_lock = Lock()
_scan_in_progress = threading.Event()
_scan_lock = Lock()
_pending_rescans: set[str] = set()
_pending_rescans_lock = Lock()


def _scan_one_truck(imei: str, lat=None, lng=None):
    state = _derive_event_state(imei)
    with _corridor_cache_lock:
        _corridor_cache[imei] = {
            "last_event_state": state,
            "scanned_at": datetime.now(timezone.utc),
            "scanned_lat": lat, "scanned_lng": lng,
        }


def _scan_one_truck_deduped(imei: str, lat, lng):
    try:
        _scan_one_truck(imei, lat, lng)
    finally:
        with _pending_rescans_lock:
            _pending_rescans.discard(imei)


def _background_scan_all(imeis: list[str]):
    _scan_in_progress.set()
    try:
        log.info("live_fleet: warming corridor cache for %d trucks", len(imeis))
        for imei in imeis:
            try:
                _scan_one_truck(imei)
            except Exception as exc:
                log.warning("warm-up scan failed for %s: %s", imei, exc)
    finally:
        _scan_in_progress.clear()


def _schedule_rescan(imei: str, lat, lng):
    with _pending_rescans_lock:
        if imei in _pending_rescans:
            return
        _pending_rescans.add(imei)
    threading.Thread(
        target=_scan_one_truck_deduped,
        args=(imei, lat, lng), daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Fleet state
# ---------------------------------------------------------------------------

_fleet_cache_lock = Lock()
_fleet_cache: dict = {"data": None, "expires_at": 0.0}


def _resolve_yard_membership(
    *,
    alarm_state: dict | None,
    gps_yard: dict | None,
    gps_fresh: bool,
) -> dict | None:
    if gps_fresh:
        if gps_yard is not None:
            return {
                "key":       gps_yard["key"],
                "label":     gps_yard["label"],
                "name":      gps_yard["fence_name"],
                "radius_km": gps_yard.get("radius_km"),
                "source":    "gps",
            }
        return None
    return alarm_state


def get_fleet_state(
    force_refresh: bool = False,
    *,
    session_id: str | None = None,
    scanning_allowed: bool = True,
) -> tuple[list[dict], bool, bool, dict]:
    """Return (states, scanning_in_progress, paused, diagnostics).

    `scanning_allowed` should be False when the *global* pause is on.
    When False, this function NEVER calls Tracksolid; it serves the last
    cached snapshot (or an empty list if there isn't one yet). The blueprint
    derives `scanning_allowed` from `is_scanning()` so admin and non-admin
    always agree.

    `session_id` is kept for API compatibility and otherwise unused.
    """
    now = time.time()

    diagnostics: dict = {
        "devices_total":        None,
        "devices_regular":      None,
        "locations_fetched":    None,
        "yard_states_resolved": None,
        "fences_error":         None,
        "reason":               None,
    }

    if not scanning_allowed:
        with _fleet_cache_lock:
            cached = _fleet_cache["data"]
            expires = _fleet_cache["expires_at"]
        if cached is not None:
            diagnostics["reason"] = "paused (cached snapshot)"
            diagnostics["snapshot_age_sec"] = (
                max(0.0, now - (expires - CACHE_TTL_SEC))
                if expires else None
            )
            return cached, False, True, diagnostics
        diagnostics["reason"] = "paused (no snapshot yet)"
        return [], False, True, diagnostics

    # --- Facilities ---
    corridor_facilities, yard_facilities, fences_error = _load_facilities_diagnostic()
    diagnostics["fences_error"] = fences_error

    with _fleet_cache_lock:
        if (not force_refresh
                and _fleet_cache["data"] is not None
                and now < _fleet_cache["expires_at"]):
            diagnostics["reason"] = "cache hit"
            return _fleet_cache["data"], _scan_in_progress.is_set(), False, diagnostics

    # --- Devices ---
    try:
        all_devices = get_devices()
    except Exception as exc:
        log.exception("live_fleet: get_devices() failed")
        diagnostics["reason"] = f"get_devices failed: {type(exc).__name__}: {exc}"
        with _fleet_cache_lock:
            cached = _fleet_cache["data"]
        if cached is not None:
            return cached, False, False, diagnostics
        return [], False, False, diagnostics

    diagnostics["devices_total"] = len(all_devices)
    devices = [
        d for d in all_devices
        if classify_device(d.get("deviceName") or "") == "regular"
    ]
    diagnostics["devices_regular"] = len(devices)

    if not devices:
        diagnostics["reason"] = (
            f"no 'regular' devices among {len(all_devices)} total"
        )
        return [], False, False, diagnostics

    imeis = [d["imei"] for d in devices]

    try:
        loc_by_imei = _fetch_locations_batched(imeis)
    except Exception as exc:
        log.exception("live_fleet: _fetch_locations_batched failed")
        diagnostics["reason"] = f"location fetch failed: {type(exc).__name__}: {exc}"
        loc_by_imei = {}
    diagnostics["locations_fetched"] = len(loc_by_imei)

    try:
        yard_state = _refresh_yard_state(imeis, yard_facilities)
    except Exception as exc:
        log.exception("live_fleet: _refresh_yard_state failed")
        yard_state = {}
    diagnostics["yard_states_resolved"] = sum(
        1 for v in yard_state.values() if v is not None
    )

    now_dt = datetime.now(timezone.utc)

    with _corridor_cache_lock:
        if len(_corridor_cache) == 0 and not _scan_in_progress.is_set():
            with _scan_lock:
                if not _scan_in_progress.is_set():
                    threading.Thread(
                        target=_background_scan_all,
                        args=(imeis,), daemon=True,
                    ).start()

    out: list[dict] = []

    for dev in devices:
        imei = dev["imei"]
        name = dev.get("deviceName") or imei
        loc = loc_by_imei.get(imei) or {}

        lat = lng = None
        speed = 0.0
        heading = None
        gps_time = None
        try:
            if loc.get("lat"):       lat = float(loc["lat"])
            if loc.get("lng"):       lng = float(loc["lng"])
            if loc.get("speed"):     speed = float(loc["speed"])
            if loc.get("direction"): heading = float(loc["direction"])
            if loc.get("gpsTime"):
                gps_time = datetime.fromisoformat(
                    loc["gpsTime"].replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

        gps_fresh = (gps_time is not None
                     and (now_dt - gps_time) <= _YARD_GPS_TRUST_AGE)

        with _corridor_cache_lock:
            cache_entry = _corridor_cache.get(imei)

        last_event_state = None
        scan_pending = True
        if cache_entry:
            last_event_state = cache_entry.get("last_event_state")
            scan_pending = False
            age = now_dt - cache_entry["scanned_at"]
            moved = 0.0
            if (lat is not None and lng is not None
                    and cache_entry.get("scanned_lat") is not None
                    and cache_entry.get("scanned_lng") is not None):
                moved = _haversine_km(
                    cache_entry["scanned_lat"], cache_entry["scanned_lng"],
                    lat, lng,
                )
            if age > _CORRIDOR_STATE_TTL and (moved > 0.5 or moved == 0):
                _schedule_rescan(imei, lat, lng)

        inside = (_inside_facility(lat, lng, corridor_facilities)
                  if lat is not None and lng is not None else None)

        if inside is not None:
            direction = f"at_{inside['kind']}"
        elif speed > MOVING_KPH and heading is not None:
            if 315 <= heading or heading <= 45:
                direction = "heading_to_awaso"
            elif 135 <= heading <= 225:
                direction = "heading_to_takoradi"
            elif last_event_state:
                direction = last_event_state
            else:
                direction = "unknown"
        else:
            direction = last_event_state or "unknown"

        motion = _motion_state(speed, gps_time, now_dt)

        gps_yard = (_inside_yard(lat, lng, yard_facilities)
                    if lat is not None and lng is not None else None)
        fence_payload = _resolve_yard_membership(
            alarm_state=yard_state.get(imei),
            gps_yard=gps_yard,
            gps_fresh=gps_fresh,
        )

        out.append({
            "imei":         imei,
            "name":         name,
            "lat":          lat,
            "lng":          lng,
            "speed_kph":    round(speed, 1),
            "direction":    direction,
            "motion":       motion,
            "fence":        fence_payload,
            "scan_pending": scan_pending,
            "gps_fresh":    gps_fresh,
        })

    with _fleet_cache_lock:
        _fleet_cache["data"] = out
        _fleet_cache["expires_at"] = now + CACHE_TTL_SEC

    diagnostics["reason"] = "fetched"
    return out, _scan_in_progress.is_set(), False, diagnostics


def summarise(states: list[dict]) -> dict:
    counts = {
        "heading_to_takoradi": 0, "heading_to_awaso": 0,
        "at_takoradi": 0, "at_awaso": 0, "unknown": 0,
    }
    motion = {"moving": 0, "idle": 0, "parked": 0, "offline": 0}
    fences = {spec["key"]: 0 for spec in _YARD_FENCES}

    for s in states:
        counts[s["direction"]] = counts.get(s["direction"], 0) + 1
        motion[s["motion"]] = motion.get(s["motion"], 0) + 1
        f = s.get("fence")
        if f and f.get("key") in fences:
            fences[f["key"]] += 1

    return {
        "direction": counts,
        "motion":    motion,
        "fences":    fences,
        "total":     len(states),
    }


# ---------------------------------------------------------------------------
# Fence saving / deleting
# ---------------------------------------------------------------------------

def _derive_name_and_kind(item: dict) -> tuple[str, str]:
    name = (item.get("name") or "").strip()
    if name:
        if name.upper() in _YARD_KEY_BY_NAME:
            return name, "yard"
        if name.strip().lower() in _CORRIDOR_KEYS:
            return name, name.strip().lower()
        return name, "custom"

    key = item.get("key")
    if key:
        if key in _YARD_KEY_BY_NAME.values():
            spec = next(s for s in _YARD_FENCES if s["key"] == key)
            return spec["fence_name"], "yard"
        if key in _CORRIDOR_KEYS:
            return _CORRIDOR_LABELS[key], key
        return key.upper(), "custom"

    return f"CUSTOM {uuid4().hex[:6].upper()}", "custom"


def save_fences(payload: list[dict]) -> dict:
    if not isinstance(payload, list) or not payload:
        raise ValueError("fences must be a non-empty list")

    client = admin_client()
    saved = 0

    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each fence must be an object")

        shape = (item.get("shape") or "polygon").strip().lower()
        if shape not in ("circle", "polygon"):
            raise ValueError(f"invalid shape: {shape!r}")

        name, kind = _derive_name_and_kind(item)

        record: dict = {
            "name":       name,
            "kind":       kind,
            "shape":      shape,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        if shape == "polygon":
            poly = _validate_polygon(item.get("polygon"))
            if poly is None:
                raise ValueError("invalid polygon (need >=3 non-degenerate vertices)")
            clat, clng = _polygon_centroid(poly)
            rkm = max(_polygon_bounding_radius_km(poly), 0.001)
            record.update({
                "polygon":   poly,
                "lat":       clat,
                "lng":       clng,
                "radius_km": rkm,
            })
        else:
            try:
                lat = float(item.get("lat"))
                lng = float(item.get("lng"))
                radius_km = float(item.get("radius_km"))
            except (TypeError, ValueError):
                raise ValueError("circle requires lat, lng, radius_km")
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                raise ValueError("lat/lng out of range")
            if radius_km <= 0:
                raise ValueError("radius_km must be positive")
            record.update({
                "lat":       lat,
                "lng":       lng,
                "radius_km": radius_km,
                "polygon":   None,
            })

        row_id = item.get("id")
        if row_id:
            resp = (
                client.table("fence_polygons")
                .update(record)
                .eq("id", row_id)
                .execute()
            )
            if not (resp.data or []):
                raise ValueError(f"fence id not found: {row_id}")
            saved += 1
        else:
            record["created_at"] = record["updated_at"]
            resp = client.table("fence_polygons").insert(record).execute()
            if not (resp.data or []):
                raise ValueError("insert returned no row")
            saved += 1

    invalidate_facilities_cache()
    corridor, yards = _load_facilities()
    return {"saved": saved, "yards": yards, "corridor": corridor}


def delete_fence(row_id: str) -> bool:
    if not row_id:
        raise ValueError("fence id is required")
    client = admin_client()
    resp = (
        client.table("fence_polygons")
        .delete()
        .eq("id", row_id)
        .execute()
    )
    deleted = len(resp.data or []) > 0
    invalidate_facilities_cache()
    return deleted