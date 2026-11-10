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
  cache TTL). When True, get_fleet_state() returns the cached snapshot
  without calling Tracksolid, and *everyone* sees the same frozen frame.
  Only admins can flip the flag via /live/api/scan.

Viewer presence (single-cache for the whole process):
  Every /live/api/fleet and /live/api/heartbeat call records a heartbeat
  for the calling session. When no session has heartbeated in
  _VIEWER_TTL_SEC, get_fleet_state() returns the cached snapshot instead
  of fetching, even if the global pause is off. This gives a "one-person
  cache": one shared poll cycle regardless of viewer count, and zero
  Tracksolid calls when nobody is watching.

Yard-alarm polling:
  get_fleet_state() calls _refresh_yard_state() which re-reads the last
  24h of alarms for any truck whose yard cache is stale (60s TTL).
  The corridor cache (last GBC fence out event) is warmed in the
  background the first time it's empty.

Yard membership has THREE freshness windows:
  * _YARD_GPS_TRUST_AGE (15 min) — how old a fix may be for the
    *corridor direction* logic to trust it for "which way is it going".
  * _YARD_GPS_MEMBERSHIP_AGE (4 h) — how old a fix may be for the
    *yard membership* test, PROVIDED the truck is parked. A parked
    truck's last fix still tells us which polygon it's inside, even
    if it's hours old. This lets us resolve membership by geometry
    when Tracksolid's own geofence is smaller than our app's polygon.
  * _YARD_SANITY_DISTANCE_KM (5 km) — if a truck's GPS says it is more
    than this far from every polygon that its alarm state claims it is
    inside, the alarm state is discarded regardless of age. This is the
    direct fix for "truck 250 km away still shows as in the fence".

Direction classification (updated Nov 2026):
  A truck is `at_<facility>` when:
    - its GPS fix is inside the corridor facility polygon, OR
    - its GPS fix is within _FACILITY_NEAR_KM of the corridor facility
      polygon AND it is not moving above _FACILITY_NEAR_MOVING_KPH.

  A truck is `heading_to_<facility>` when its bearing (from the live fix)
  falls within ±_CORRIDOR_BEARING_TOLERANCE_DEG of the corridor axis
  bearing toward that facility. The corridor axis is computed from the
  two facility centroids.

  The Awaso↔Takoradi road is not straight — it curves through towns and
  bends. A tight ±45° cone around the straight-line axis leaves 40-50%
  of a legitimately corridor-bound truck's track points classified as
  off-corridor. The tolerance is therefore ±60°, which matches what
  Tracksolid's own playback shows.

  When a moving truck's bearing momentarily falls outside the ±60° cone
  (sharp bend, brief GPS glitch, or slow manoeuvre inside a town), we do
  NOT immediately flip to "unknown". Instead we hold the last known
  corridor direction, provided the truck has moved less than
  _CORRIDOR_HOLD_DISTANCE_KM (5 km) since that classification was made.
  This is the "direction memory" Tracksolid's playback has and which a
  naive bearing-vs-axis classifier lacks. Only a genuine 5 km+ departure
  from the corridor resets the direction to "unknown".
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

# How long a stale alarm `in` is still trusted if GPS can't contradict it.
_YARD_STATE_MAX_AGE        = timedelta(hours=4)

# Sanity distance for the yard membership test.
_YARD_SANITY_DISTANCE_KM   = 5.0

# Two separate freshness windows for the corridor direction logic.
_YARD_GPS_TRUST_AGE        = timedelta(minutes=15)
_YARD_GPS_MEMBERSHIP_AGE   = timedelta(hours=4)

# "At facility" heuristic: within this many km of a corridor facility and
# not moving faster than _FACILITY_NEAR_MOVING_KPH → counted as AT that
# facility, not "heading to" the other one.
_FACILITY_NEAR_KM          = 5.0
_FACILITY_NEAR_MOVING_KPH  = 20.0

# ---------------------------------------------------------------------------
# Corridor direction tuning (Nov 2026)
#
# The road is not a straight line; a ±45° cone around the straight-line
# corridor axis misses almost half the track points. ±60° matches the
# playback tab's classification. When a truck's bearing is momentarily
# outside that cone (bend, manoeuvre, GPS glitch), we hold the last
# corridor direction as long as the truck is still within
# _CORRIDOR_HOLD_DISTANCE_KM of where that direction was last confirmed.
# ---------------------------------------------------------------------------
_CORRIDOR_BEARING_TOLERANCE_DEG = 60.0
_CORRIDOR_HOLD_DISTANCE_KM      = 5.0
_CORRIDOR_HOLD_MAX_AGE          = timedelta(minutes=30)

# Viewer presence
_VIEWER_TTL_SEC            = 60

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

_paused: bool = False
_paused_since: datetime | None = None
_paused_lock = Lock()


def is_scanning(session_id: str | None = None, default: bool = True) -> bool:
    with _paused_lock:
        return not _paused


def set_scanning(session_id: str | None, enabled: bool) -> None:
    global _paused, _paused_since
    with _paused_lock:
        _paused = not bool(enabled)
        if _paused:
            if _paused_since is None:
                _paused_since = datetime.now(timezone.utc)
        else:
            _paused_since = None


def paused_since() -> datetime | None:
    with _paused_lock:
        return _paused_since


# ---------------------------------------------------------------------------
# Viewer presence (single-cache for the whole process)
# ---------------------------------------------------------------------------

_viewers: dict[str, float] = {}
_viewers_lock = Lock()


def heartbeat(session_id: str | None) -> None:
    if not session_id:
        return
    with _viewers_lock:
        _viewers[session_id] = time.time()


def viewer_count() -> int:
    cutoff = time.time() - _VIEWER_TTL_SEC
    with _viewers_lock:
        stale = [k for k, t in _viewers.items() if t < cutoff]
        for k in stale:
            _viewers.pop(k, None)
        return len(_viewers)


def has_active_viewers() -> bool:
    return viewer_count() > 0


def forget_session(session_id: str | None) -> None:
    if not session_id:
        return
    with _viewers_lock:
        _viewers.pop(session_id, None)


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


def _bearing_deg(lat1: float, lng1: float,
                 lat2: float, lng2: float) -> float:
    """Initial bearing from (lat1,lng1) to (lat2,lng2), 0°=N, clockwise."""
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def _angular_distance(a: float, b: float) -> float:
    """Smallest angular distance between two bearings, in degrees (0-180)."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


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


def _point_to_segment_km(lat: float, lng: float,
                         lat1: float, lng1: float,
                         lat2: float, lng2: float) -> float:
    ref_lat = math.radians((lat + lat1 + lat2) / 3.0)
    kx = 111.32 * math.cos(ref_lat)
    ky = 111.32

    ax = (lng1 - lng) * kx
    ay = (lat1 - lat) * ky
    bx = (lng2 - lng) * kx
    by = (lat2 - lat) * ky

    dx = bx - ax
    dy = by - ay
    if dx == 0.0 and dy == 0.0:
        return math.hypot(ax, ay)
    t = -(ax * dx + ay * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    cx = ax + t * dx
    cy = ay + t * dy
    return math.hypot(cx, cy)


def _distance_to_facility_km(lat: float, lng: float, facility: dict) -> float:
    if facility.get("shape") == "polygon" and facility.get("polygon"):
        poly = facility["polygon"]
        if _point_in_polygon(lat, lng, poly):
            return 0.0
        best = float("inf")
        n = len(poly)
        for i in range(n):
            y1, x1 = poly[i]
            y2, x2 = poly[(i + 1) % n]
            d = _point_to_segment_km(lat, lng, y1, x1, y2, x2)
            if d < best:
                best = d
        return best

    if facility.get("shape") == "circle" and facility.get("radius_km") is not None:
        d = _haversine_km(lat, lng, facility["lat"], facility["lng"])
        return max(0.0, d - facility["radius_km"])

    return float("inf")


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


def _nearest_facility(lat: float, lng: float,
                      facilities: list[dict]) -> tuple[dict | None, float]:
    best: dict | None = None
    best_d = float("inf")
    for f in facilities:
        if f.get("shape") not in ("polygon", "circle"):
            continue
        d = _distance_to_facility_km(lat, lng, f)
        if d < best_d:
            best_d = d
            best = f
    return best, best_d


# ---------------------------------------------------------------------------
# Corridor axis + bearing
# ---------------------------------------------------------------------------
#
# Computed once from the two facility centroids and cached until the
# facilities change. The two bearings are the straight-line axis in each
# direction; the tolerance in _CORRIDOR_BEARING_TOLERANCE_DEG widens the
# cone to account for road curves.

_corridor_axis_lock = Lock()
_corridor_axis_cache: dict = {
    "awaso_lat": None, "awaso_lng": None,
    "takoradi_lat": None, "takoradi_lng": None,
    "to_takoradi_bearing": None,
    "to_awaso_bearing": None,
}


def _corridor_axis(corridor_facilities: list[dict]) -> dict:
    """Return {to_takoradi_bearing, to_awaso_bearing} from facility centroids."""
    awaso = next(
        (f for f in corridor_facilities if f.get("kind") == "awaso"),
        None,
    )
    takoradi = next(
        (f for f in corridor_facilities if f.get("kind") == "takoradi"),
        None,
    )
    if not awaso or not takoradi:
        return {
            "to_takoradi_bearing": None,
            "to_awaso_bearing": None,
        }

    # Check cache
    with _corridor_axis_lock:
        if (_corridor_axis_cache["awaso_lat"] == awaso.get("lat")
                and _corridor_axis_cache["awaso_lng"] == awaso.get("lng")
                and _corridor_axis_cache["takoradi_lat"] == takoradi.get("lat")
                and _corridor_axis_cache["takoradi_lng"] == takoradi.get("lng")
                and _corridor_axis_cache["to_takoradi_bearing"] is not None):
            return {
                "to_takoradi_bearing": _corridor_axis_cache["to_takoradi_bearing"],
                "to_awaso_bearing": _corridor_axis_cache["to_awaso_bearing"],
            }

    try:
        to_tak = _bearing_deg(
            float(awaso["lat"]), float(awaso["lng"]),
            float(takoradi["lat"]), float(takoradi["lng"]),
        )
    except (TypeError, ValueError):
        return {
            "to_takoradi_bearing": None,
            "to_awaso_bearing": None,
        }
    to_awa = (to_tak + 180.0) % 360.0

    with _corridor_axis_lock:
        _corridor_axis_cache.update({
            "awaso_lat": awaso.get("lat"),
            "awaso_lng": awaso.get("lng"),
            "takoradi_lat": takoradi.get("lat"),
            "takoradi_lng": takoradi.get("lng"),
            "to_takoradi_bearing": to_tak,
            "to_awaso_bearing": to_awa,
        })

    return {
        "to_takoradi_bearing": to_tak,
        "to_awaso_bearing": to_awa,
    }


def _direction_from_heading_corridor(
    heading: float,
    axis: dict,
    *,
    tolerance_deg: float = _CORRIDOR_BEARING_TOLERANCE_DEG,
) -> str | None:
    """Classify a bearing against the corridor axis.

    Returns 'heading_to_takoradi', 'heading_to_awaso', or None if the
    bearing is outside both cones.
    """
    to_tak = axis.get("to_takoradi_bearing")
    to_awa = axis.get("to_awaso_bearing")
    if to_tak is None or to_awa is None:
        return None
    if _angular_distance(heading, to_tak) <= tolerance_deg:
        return "heading_to_takoradi"
    if _angular_distance(heading, to_awa) <= tolerance_deg:
        return "heading_to_awaso"
    return None


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
    now = datetime.now(timezone.utc)

    last_event: dict[str, str] = {}
    last_in_time: dict[str, datetime | None] = {}

    for a in alarms:
        name = _alarm_fence_name(a).upper()
        key = _YARD_KEY_BY_NAME.get(name)
        if key is None:
            continue
        ev = _alarm_event(a)
        if ev is None:
            continue
        if key in last_event:
            continue
        last_event[key] = ev
        last_in_time[key] = _alarm_time(a) if ev == "in" else None

    inside: list[tuple[datetime, str]] = []
    for key, ev in last_event.items():
        if ev != "in":
            continue
        t = last_in_time.get(key)
        if t is not None and (now - t) > _YARD_STATE_MAX_AGE:
            log.debug("yard-state: ignoring stale 'in' for %s at %s", key, t)
            continue
        inside.append((t or datetime.min.replace(tzinfo=timezone.utc), key))

    if not inside:
        return None

    inside.sort(key=lambda x: x[0], reverse=True)
    _, chosen_key = inside[0]
    spec = next(s for s in _YARD_FENCES if s["key"] == chosen_key)
    t = last_in_time.get(chosen_key)
    return {
        "key":       chosen_key,
        "label":     spec["label"],
        "name":      spec["fence_name"],
        "radius_km": None,
        "source":    "alarm",
        "since":     t.isoformat() if t else None,
    }


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
    gps_membership_ok: bool,
    gps_time: datetime | None = None,
    gps_lat: float | None = None,
    gps_lng: float | None = None,
    yard_facilities: list[dict] | None = None,
) -> dict | None:
    alarm_time = None
    if alarm_state and alarm_state.get("since"):
        try:
            alarm_time = datetime.fromisoformat(alarm_state["since"])
            if alarm_time.tzinfo is None:
                alarm_time = alarm_time.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            alarm_time = None

    # --- Step 1: fresh GPS outside beats stale alarm-in ---
    if gps_membership_ok and gps_yard is None and alarm_state is not None:
        if gps_time is not None and alarm_time is not None and alarm_time <= gps_time:
            log.debug(
                "membership: GPS outside beats stale alarm-in for %s "
                "(alarm %s, gps %s)",
                alarm_state.get("key"), alarm_time, gps_time,
            )
            return None

    # --- Step 2: alarm-in newer than the fix ---
    if (alarm_state is not None
            and alarm_time is not None
            and (gps_time is None or alarm_time > gps_time)):
        if (gps_lat is not None and gps_lng is not None
                and yard_facilities):
            claimed = next(
                (y for y in yard_facilities
                 if y.get("key") == alarm_state.get("key")),
                None,
            )
            if claimed is not None and claimed.get("shape") in ("polygon", "circle"):
                dist = _distance_to_facility_km(gps_lat, gps_lng, claimed)
                if dist > _YARD_SANITY_DISTANCE_KM:
                    log.info(
                        "membership: dropping alarm-in for %s — truck is "
                        "%.1f km from the polygon (max %.1f)",
                        alarm_state.get("key"), dist, _YARD_SANITY_DISTANCE_KM,
                    )
                    alarm_state = None
                else:
                    return alarm_state
            else:
                return alarm_state
        else:
            return alarm_state

    # --- Step 3: GPS inside wins ---
    if gps_membership_ok and gps_yard is not None:
        return {
            "key":       gps_yard["key"],
            "label":     gps_yard["label"],
            "name":      gps_yard["fence_name"],
            "radius_km": gps_yard.get("radius_km"),
            "source":    "gps",
        }

    # --- Step 4: fall back to alarm state, with distance sanity check ---
    if alarm_state is not None:
        if (gps_lat is not None and gps_lng is not None
                and yard_facilities):
            claimed = next(
                (y for y in yard_facilities
                 if y.get("key") == alarm_state.get("key")),
                None,
            )
            if claimed is not None and claimed.get("shape") in ("polygon", "circle"):
                dist = _distance_to_facility_km(gps_lat, gps_lng, claimed)
                if dist > _YARD_SANITY_DISTANCE_KM:
                    log.debug(
                        "membership: falling back to alarm-in for %s but "
                        "truck is %.1f km away — dropping",
                        alarm_state.get("key"), dist,
                    )
                    return None
        return alarm_state

    return None


# ---------------------------------------------------------------------------
# Direction hold cache (Nov 2026)
#
# When a moving truck's bearing falls outside the ±60° cone momentarily
# (bend, manoeuvre, brief GPS glitch), hold the last confirmed direction
# if the truck has moved less than _CORRIDOR_HOLD_DISTANCE_KM since that
# direction was confirmed. This mirrors Tracksolid's playback behaviour.
# ---------------------------------------------------------------------------

_direction_hold_cache: dict[str, dict] = {}
_direction_hold_lock = Lock()


def _direction_hold_get(imei: str) -> dict | None:
    with _direction_hold_lock:
        return _direction_hold_cache.get(imei)


def _direction_hold_set(imei: str, direction: str,
                        lat: float | None, lng: float | None) -> None:
    if direction not in ("heading_to_takoradi", "heading_to_awaso"):
        return
    with _direction_hold_lock:
        _direction_hold_cache[imei] = {
            "direction": direction,
            "lat":       lat,
            "lng":       lng,
            "set_at":    datetime.now(timezone.utc),
        }


def _direction_hold_clear(imei: str) -> None:
    with _direction_hold_lock:
        _direction_hold_cache.pop(imei, None)


def _classify_direction(
    *,
    imei: str,
    inside: dict | None,
    near_facility: dict | None,
    near_dist: float,
    speed: float,
    heading: float | None,
    gps_fresh: bool,
    last_event_state: str | None,
    axis: dict,
    lat: float | None,
    lng: float | None,
    now_dt: datetime,
) -> str:
    """Return the best-guess corridor direction for one truck.

    Priority (highest first):
      1. Inside a facility polygon → at_<kind>
      2. Near a facility + not moving fast → at_<kind>
      3. Moving with a bearing in the ±60° cone → heading_to_<facility>
      4. Moving, bearing ambiguous, but held direction is still fresh
         and truck hasn't moved far → hold previous direction
      5. Fall back to last corridor alarm state
      6. Unknown
    """

    # 1. Inside a facility
    if inside is not None:
        direction = f"at_{inside['kind']}"
        # If we were "heading" and just arrived, clear the hold.
        if near_dist == 0.0:
            _direction_hold_clear(imei)
        return direction

    # 2. Near facility + slow
    if (near_facility is not None
            and near_dist <= _FACILITY_NEAR_KM
            and speed < _FACILITY_NEAR_MOVING_KPH):
        return f"at_{near_facility['kind']}"

    # 3. Moving with bearing in the cone
    moving_now = (speed > MOVING_KPH and heading is not None and gps_fresh)
    if moving_now:
        heading_dir = _direction_from_heading_corridor(heading, axis)
        if heading_dir:
            _direction_hold_set(imei, heading_dir, lat, lng)
            return heading_dir

        # 4. Bearing ambiguous — try the hold
        hold = _direction_hold_get(imei)
        if hold is not None:
            age = now_dt - hold["set_at"]
            if age <= _CORRIDOR_HOLD_MAX_AGE:
                if (hold["lat"] is not None and hold["lng"] is not None
                        and lat is not None and lng is not None):
                    moved_km = _haversine_km(
                        hold["lat"], hold["lng"], lat, lng,
                    )
                    if moved_km <= _CORRIDOR_HOLD_DISTANCE_KM:
                        # Extend the hold with the current position so it
                        # survives another bend.
                        _direction_hold_set(imei, hold["direction"], lat, lng)
                        return hold["direction"]
                    else:
                        log.debug(
                            "direction hold expired for %s: moved %.1f km "
                            "since last confirmed heading",
                            imei, moved_km,
                        )
                        _direction_hold_clear(imei)
                else:
                    # No coordinates to compare, but the hold is still
                    # fresh — trust it for one more tick.
                    return hold["direction"]
            else:
                _direction_hold_clear(imei)

    # 5. Fall back to last corridor alarm state
    if last_event_state:
        _direction_hold_set(imei, last_event_state, lat, lng)
        return last_event_state

    # 6. Unknown
    return "unknown"


def get_fleet_state(
    force_refresh: bool = False,
    *,
    session_id: str | None = None,
    scanning_allowed: bool = True,
) -> tuple[list[dict], bool, bool, dict]:
    """Return (states, scanning_in_progress, paused, diagnostics)."""
    now = time.time()

    diagnostics: dict = {
        "devices_total":        None,
        "devices_regular":      None,
        "locations_fetched":    None,
        "yard_states_resolved": None,
        "fences_error":         None,
        "viewer_count":         viewer_count(),
        "reason":               None,
    }

    # ---- viewer presence / pause gate ----
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

    if not has_active_viewers():
        with _fleet_cache_lock:
            cached = _fleet_cache["data"]
            expires = _fleet_cache["expires_at"]
        if cached is not None:
            diagnostics["reason"] = "no active viewers (cached snapshot)"
            diagnostics["snapshot_age_sec"] = (
                max(0.0, now - (expires - CACHE_TTL_SEC))
                if expires else None
            )
            return cached, _scan_in_progress.is_set(), False, diagnostics
        diagnostics["reason"] = "no active viewers (no snapshot yet)"
        return [], False, False, diagnostics

    corridor_facilities, yard_facilities, fences_error = _load_facilities_diagnostic()
    diagnostics["fences_error"] = fences_error
    axis = _corridor_axis(corridor_facilities)

    with _fleet_cache_lock:
        if (not force_refresh
                and _fleet_cache["data"] is not None
                and now < _fleet_cache["expires_at"]):
            diagnostics["reason"] = "cache hit"
            return _fleet_cache["data"], _scan_in_progress.is_set(), False, diagnostics

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

        gps_membership_ok = (
            gps_time is not None
            and (
                (now_dt - gps_time) <= _YARD_GPS_TRUST_AGE
                or (speed < MOVING_KPH
                    and (now_dt - gps_time) <= _YARD_GPS_MEMBERSHIP_AGE)
            )
        )

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

        near_facility = None
        near_dist = float("inf")
        if lat is not None and lng is not None:
            near_facility, near_dist = _nearest_facility(
                lat, lng, corridor_facilities
            )

        direction = _classify_direction(
            imei=imei,
            inside=inside,
            near_facility=near_facility,
            near_dist=near_dist,
            speed=speed,
            heading=heading,
            gps_fresh=gps_fresh,
            last_event_state=last_event_state,
            axis=axis,
            lat=lat,
            lng=lng,
            now_dt=now_dt,
        )

        motion = _motion_state(speed, gps_time, now_dt)

        gps_yard = (_inside_yard(lat, lng, yard_facilities)
                    if lat is not None and lng is not None else None)
        fence_payload = _resolve_yard_membership(
            alarm_state=yard_state.get(imei),
            gps_yard=gps_yard,
            gps_membership_ok=gps_membership_ok,
            gps_time=gps_time,
            gps_lat=lat,
            gps_lng=lng,
            yard_facilities=yard_facilities,
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
    # Corridor axis cache invalidation — a facility may have moved.
    with _corridor_axis_lock:
        _corridor_axis_cache.update({
            "awaso_lat": None, "awaso_lng": None,
            "takoradi_lat": None, "takoradi_lng": None,
            "to_takoradi_bearing": None,
            "to_awaso_bearing": None,
        })
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
    with _corridor_axis_lock:
        _corridor_axis_cache.update({
            "awaso_lat": None, "awaso_lng": None,
            "takoradi_lat": None, "takoradi_lng": None,
            "to_takoradi_bearing": None,
            "to_awaso_bearing": None,
        })
    return deleted