"""
services/live_fleet.py — Live Awaso↔Takoradi corridor view.

Rules (strict priority):
1. GPS Radius. If inside a facility -> at_<kind>. This is the ONLY way at_* can be produced.
2. GPS Heading (only if moving > 5 kph and outside circles).
   - 315-45°  -> heading_to_awaso
   - 135-225° -> heading_to_takoradi
3. Alarm events (only if parked outside circles). Can ONLY return heading_to_*, never at_*.
   - GBC EXITED (out)  -> heading_to_takoradi
   - TAKORADI ENTRY (out) -> heading_to_awaso
   - 'TRIP TIME' fences are ignored entirely (they cover the whole corridor).
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from threading import Lock

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

# Facilities list refreshes fast so radius edits in Supabase take effect quickly.
_FACILITIES_TTL_SEC = 30

# ---------------------------------------------------------------------------
# Facility definitions
# ---------------------------------------------------------------------------

_facilities_lock = Lock()
_facilities_cache: dict = {"data": None, "expires_at": 0.0}

def _load_facilities() -> tuple[list[dict]]:
    now = time.time()
    with _facilities_lock:
        if (_facilities_cache["data"] is not None
                and now < _facilities_cache["expires_at"]):
            return _facilities_cache["data"]

    client = admin_client()
    try:
        fac_resp = client.table("fence_polygons").select("*").execute()
    except Exception as exc:
        log.error("Failed to load fence_polygons: %s", exc)
        # Keep old cache if DB is down but we already have data.
        with _facilities_lock:
            if _facilities_cache["data"] is not None:
                return _facilities_cache["data"]
        return ([],)

    facilities = [
        {
            "geoid":     r["geoid"],
            "name":      r["name"],
            "kind":      r["kind"],
            "lat":       float(r["lat"]),
            "lng":       float(r["lng"]),
            "radius_km": float(r["radius_km"]),
        }
        for r in (fac_resp.data or [])
    ]

    payload = (facilities,)
    with _facilities_lock:
        _facilities_cache["data"] = payload
        _facilities_cache["expires_at"] = now + _FACILITIES_TTL_SEC

    log.info("live_fleet: facilities refreshed — %s",
             ", ".join(f"{f['name']} ({f['radius_km']} km)" for f in facilities))
    return payload

# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------

def _haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0088
    p1 = math.radians(lat1); p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))

def _inside_facility(lat: float, lng: float, facilities: list[dict]) -> dict | None:
    for f in facilities:
        if _haversine_km(lat, lng, f["lat"], f["lng"]) <= f["radius_km"]:
            return f
    return None

# ---------------------------------------------------------------------------
# Alarm Processing — only produces heading_to_* states
# ---------------------------------------------------------------------------

_FENCE_SUFFIX_RE = re.compile(r"\(([^()]*)\)\s*$")

def _fence_name(alarm: dict) -> str:
    name = (alarm.get("alarmTypeName") or "").strip()
    m = _FENCE_SUFFIX_RE.search(name)
    return m.group(1).strip() if m else ""

def _derive_event_state(imei: str, lookback_days: int = CORRIDOR_LOOKBACK_DAYS) -> str | None:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=lookback_days)

    try:
        r = api_call("jimi.device.alarm.list", {
            "imeis": imei,
            "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
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
        name = _fence_name(a)
        event = (a.get("alertTypeId") or "").lower()

        if "TRIP TIME" in name:
            continue

        # Departing Awaso -> heading to Takoradi
        if name in ("GBC EXITED", "GBC EXIT", "HAULAGE TRUCKS EXIT"):
            if event == "out":
                return "heading_to_takoradi"

        # Departing Takoradi -> heading to Awaso
        if name in ("TAKORADI ENTRY", "GBC TAKORADI", "GBC TAKORADI 2"):
            if event == "out":
                return "heading_to_awaso"

    return None

# ---------------------------------------------------------------------------
# Public state model
# ---------------------------------------------------------------------------

def _motion_state(speed_kph: float, gps_time: datetime | None, now: datetime) -> str:
    if gps_time is None: return "offline"
    age_min = (now - gps_time).total_seconds() / 60.0
    if age_min > OFFLINE_MIN: return "offline"
    if speed_kph >= MOVING_KPH: return "moving"
    return "parked" if age_min > 10 else "idle"

def _fetch_locations_batched(imeis: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(imeis), _LOCATION_BATCH_SIZE):
        chunk = imeis[i:i + _LOCATION_BATCH_SIZE]
        if not chunk: continue
        try:
            r = api_call("jimi.device.location.get", {"imeis": ",".join(chunk)})
            result = r.get("result") or r.get("data") or []
            if isinstance(result, dict): result = result.get("list") or []
            for loc in result:
                if loc.get("imei"): out[loc["imei"]] = loc
        except Exception as exc:
            log.warning("location.get chunk failed: %s", exc)
    return out

# ---------------------------------------------------------------------------
# Background cache
# ---------------------------------------------------------------------------

_corridor_cache: dict[str, dict] = {}
_corridor_cache_lock = Lock()
_scan_in_progress = threading.Event()
_scan_lock = Lock()
_pending_rescans: set[str] = set()
_pending_rescans_lock = Lock()

def _scan_one_truck(imei: str, lat: float = None, lng: float = None):
    state = _derive_event_state(imei)
    with _corridor_cache_lock:
        _corridor_cache[imei] = {
            "last_event_state": state,
            "scanned_at": datetime.now(timezone.utc),
            "scanned_lat": lat, "scanned_lng": lng,
        }

def _scan_one_truck_deduped(imei: str, lat: float, lng: float):
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
        log.info("live_fleet: corridor cache warm-up complete")
    finally:
        _scan_in_progress.clear()

def _schedule_rescan(imei: str, lat: float, lng: float):
    with _pending_rescans_lock:
        if imei in _pending_rescans: return
        _pending_rescans.add(imei)
    threading.Thread(target=_scan_one_truck_deduped,
                     args=(imei, lat, lng), daemon=True).start()

# ---------------------------------------------------------------------------
# Fleet state
# ---------------------------------------------------------------------------

_fleet_cache_lock = Lock()
_fleet_cache: dict = {"data": None, "expires_at": 0.0}

def get_fleet_state(force_refresh: bool = False) -> tuple[list[dict], bool]:
    now = time.time()
    with _fleet_cache_lock:
        if (not force_refresh and _fleet_cache["data"] is not None
                and now < _fleet_cache["expires_at"]):
            return _fleet_cache["data"], _scan_in_progress.is_set()

    devices = [d for d in get_devices()
               if classify_device(d.get("deviceName") or "") == "regular"]
    if not devices:
        return [], False

    facilities = _load_facilities()[0]
    imeis = [d["imei"] for d in devices]
    loc_by_imei = _fetch_locations_batched(imeis)
    now_dt = datetime.now(timezone.utc)

    with _corridor_cache_lock:
        if len(_corridor_cache) == 0 and not _scan_in_progress.is_set():
            with _scan_lock:
                if not _scan_in_progress.is_set():
                    threading.Thread(target=_background_scan_all,
                                     args=(imeis,), daemon=True).start()

    out = []
    for dev in devices:
        imei = dev["imei"]
        name = dev.get("deviceName") or imei
        loc = loc_by_imei.get(imei) or {}

        lat, lng, speed, gps_time, heading = None, None, 0.0, None, None
        try:
            if loc.get("lat"): lat = float(loc["lat"])
            if loc.get("lng"): lng = float(loc["lng"])
            if loc.get("speed"): speed = float(loc["speed"])
            if loc.get("direction"): heading = float(loc["direction"])
            if loc.get("gpsTime"):
                gps_time = datetime.fromisoformat(
                    loc["gpsTime"].replace("Z", "+00:00")
                ).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

        with _corridor_cache_lock:
            cache_entry = _corridor_cache.get(imei)

        last_event_state = None
        scan_pending = True

        if cache_entry:
            last_event_state = cache_entry.get("last_event_state")
            scan_pending = False

            age = now_dt - cache_entry["scanned_at"]
            moved = 0
            if (lat is not None and lng is not None
                    and cache_entry.get("scanned_lat") is not None
                    and cache_entry.get("scanned_lng") is not None):
                moved = _haversine_km(cache_entry["scanned_lat"],
                                      cache_entry["scanned_lng"], lat, lng)

            if age > _CORRIDOR_STATE_TTL and (moved > 0.5 or moved == 0):
                _schedule_rescan(imei, lat, lng)

        # ---- STATE DECISION ----
        inside = (_inside_facility(lat, lng, facilities)
                  if lat is not None and lng is not None else None)

        if inside is not None:
            # Rule 1: GPS inside the circle is authoritative.
            direction = f"at_{inside['kind']}"
        elif speed > MOVING_KPH and heading is not None:
            # Rule 2: GPS heading (only when actually moving).
            if 315 <= heading or heading <= 45:
                direction = "heading_to_awaso"
            elif 135 <= heading <= 225:
                direction = "heading_to_takoradi"
            elif last_event_state:
                direction = last_event_state
            else:
                direction = "unknown"
        else:
            # Rule 3: parked outside circles — trust the last fence event.
            # NOTE: last_event_state can only ever be heading_to_awaso /
            # heading_to_takoradi / None (never at_*).
            direction = last_event_state or "unknown"

        # Sanity check: log when a truck is far from any facility but still
        # showing at_* from some other source. Should never fire now, but it
        # will catch regressions.
        if direction.startswith("at_") and inside is None:
            log.warning(
                "live_fleet: %s classified %s but GPS shows outside all "
                "facilities (lat=%s lng=%s) — this should be impossible",
                name, direction, lat, lng,
            )

        motion = _motion_state(speed, gps_time, now_dt)

        out.append({
            "imei":         imei,
            "name":         name,
            "lat":          lat,
            "lng":          lng,
            "speed_kph":    round(speed, 1),
            "direction":    direction,
            "motion":       motion,
            "scan_pending": scan_pending,
        })

    with _fleet_cache_lock:
        _fleet_cache["data"] = out
        _fleet_cache["expires_at"] = now + CACHE_TTL_SEC

    return out, _scan_in_progress.is_set()


def summarise(states: list[dict]) -> dict:
    counts = {"heading_to_takoradi": 0, "heading_to_awaso": 0,
              "at_takoradi": 0, "at_awaso": 0, "unknown": 0}
    motion = {"moving": 0, "idle": 0, "parked": 0, "offline": 0}
    for s in states:
        counts[s["direction"]] = counts.get(s["direction"], 0) + 1
        motion[s["motion"]] = motion.get(s["motion"], 0) + 1
    return {"direction": counts, "motion": motion, "total": len(states)}