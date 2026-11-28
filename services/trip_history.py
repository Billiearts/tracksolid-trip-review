"""
services/trip_history.py — completed-trip lookups from Tracksolid Pro.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

MAX_IMEIS_PER_QUERY = 300
_FETCH_WORKERS = 4
_CACHE_OK: bool | None = None


# ---------------------------------------------------------------------------
# Time coercion
# ---------------------------------------------------------------------------

def _coerce_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    try:
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Small lookups
# ---------------------------------------------------------------------------

def _driver_name_map(driver_ids):
    if not driver_ids:
        return {}
    from services.supabase_client import admin_client
    try:
        resp = (admin_client().table("drivers")
                .select("id,full_name")
                .in_("id", list({d for d in driver_ids if d}))
                .execute())
        return {r["id"]: r["full_name"] for r in (resp.data or [])}
    except Exception:
        log.exception("trip_history: driver name map failed")
        return {}


def _truck_name_map(imeis):
    try:
        from services.devices import (get_devices_cached_only, get_devices)
        try:
            devs = get_devices_cached_only()
        except Exception:
            devs = None
        if devs is None:
            devs = get_devices()
    except Exception:
        return {}
    out = {}
    for d in devs or []:
        imei = (d.get("imei") or "").strip()
        if not imei:
            continue
        out[imei] = (d.get("deviceName") or "").strip()
    return out


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def use_db_cache() -> bool:
    global _CACHE_OK
    if _CACHE_OK is not None:
        return _CACHE_OK
    from services.supabase_client import admin_client
    try:
        (admin_client().table("trip_history").select("id").limit(1).execute())
        _CACHE_OK = True
    except Exception:
        _CACHE_OK = False
    return _CACHE_OK


def _cache_trip(row: dict) -> None:
    if not use_db_cache():
        return
    from services.supabase_client import admin_client
    try:
        (admin_client().table("trip_history")
         .upsert({
             "imei":       row["imei"],
             "trip_start": row["start"].isoformat(),
             "trip_end":   row["end"].isoformat(),
             "truck_id":   row.get("truck_id") or None,
             "driver_id":  row.get("driver_id") or None,
             "distance":   row.get("distance"),
             "duration_s": int(row.get("duration_s") or 0),
             "raw":        row.get("raw") or {},
         }, on_conflict="imei,trip_start")
         .execute())
    except Exception:
        log.exception("trip_history cache upsert failed")


def _cached_trips(imei: str, begin: datetime, end: datetime) -> list[dict]:
    if not use_db_cache():
        return []
    from services.supabase_client import admin_client
    try:
        resp = (admin_client().table("trip_history")
                .select("trip_start,trip_end,truck_id,driver_id,distance,duration_s,raw")
                .eq("imei", imei)
                .gte("trip_end",  begin.isoformat())
                .lt("trip_start", end.isoformat())
                .order("trip_start", desc=False)
                .limit(500)
                .execute())
        rows = resp.data or []
    except Exception:
        return []
    out = []
    for r in rows:
        start = _coerce_dt(r.get("trip_start"))
        endd  = _coerce_dt(r.get("trip_end"))
        if start is None or endd is None:
            continue
        out.append({
            "start":      start,
            "end":        endd,
            "distance":   r.get("distance"),
            "duration_s": int(r.get("duration_s") or 0),
            "raw":        r.get("raw") or {},
        })
    return out


# ---------------------------------------------------------------------------
# Assignment attribution (NOW WITH FALLBACK)
# ---------------------------------------------------------------------------

def _assignment_windows(imei: str, begin: datetime, end: datetime) -> list[tuple]:
    from services.supabase_client import admin_client
    try:
        resp = (admin_client().table("truck_assignments")
                .select("driver_id,truck_id,assigned_at,released_at")
                .eq("imei", imei)
                .lt("assigned_at", end.isoformat())
                .or_(f"released_at.is.null,released_at.gt.{begin.isoformat()}")
                .order("assigned_at", desc=False)
                .limit(200)
                .execute())
        rows = resp.data or []
    except Exception:
        log.exception("trip_history: window lookup failed for %s", imei)
        return []
    
    out = []
    for r in rows:
        a   = _coerce_dt(r.get("assigned_at"))
        rls = _coerce_dt(r.get("released_at"))
        if a is None:
            continue
        out.append((a, rls, r.get("driver_id"), r.get("truck_id")))
    return out


def _driver_at(windows, at: datetime):
    """Return driver_id that held the truck at `at`, or the last driver before `at`."""
    if not windows:
        return None
    # Pass 1: strict window
    for a, rls, did, _tid in windows:
        if at < a: continue
        if rls is not None and at >= rls: continue
        return did
    # Pass 2: most recent assignment that started before `at`
    last_did = None
    for a, _rls, did, _tid in windows:
        if a <= at: last_did = did
        else: break
    return last_did


def _truck_at(windows, at: datetime):
    if not windows:
        return None
    for a, rls, _did, tid in windows:
        if at < a: continue
        if rls is not None and at >= rls: continue
        return tid
    last_tid = None
    for a, _rls, _did, tid in windows:
        if a <= at: last_tid = tid
        else: break
    return last_tid


# ---------------------------------------------------------------------------
# Candidate IMEIs
# ---------------------------------------------------------------------------

def _imeis_for_driver(driver_ids, begin: datetime, end: datetime) -> list[str]:
    from services.supabase_client import admin_client
    ids = [i for i in (driver_ids or []) if i]
    if not ids:
        return []
    try:
        resp = (admin_client().table("truck_assignments")
                .select("imei")
                .in_("driver_id", ids)
                .lt("assigned_at", end.isoformat())
                .or_(f"released_at.is.null,released_at.gt.{begin.isoformat()}")
                .limit(2000)
                .execute())
        rows = resp.data or []
    except Exception:
        return []
    seen, out = set(), []
    for r in rows:
        imei = (r.get("imei") or "").strip()
        if imei and imei not in seen:
            seen.add(imei)
            out.append(imei)
    return out


def _imeis_for_range(begin: datetime, end: datetime) -> list[str]:
    from services.supabase_client import admin_client
    try:
        resp = (admin_client().table("truck_assignments")
                .select("imei")
                .lt("assigned_at", end.isoformat())
                .or_(f"released_at.is.null,released_at.gt.{begin.isoformat()}")
                .limit(5000)
                .execute())
        rows = resp.data or []
    except Exception:
        return []
    seen, out = set(), []
    for r in rows:
        imei = (r.get("imei") or "").strip()
        if imei and imei not in seen:
            seen.add(imei)
            out.append(imei)
    return out


# ---------------------------------------------------------------------------
# Fetch + normalise
# ---------------------------------------------------------------------------

def _fetch_one(imei: str, begin: datetime, end: datetime) -> tuple[str, list[dict]]:
    cached = _cached_trips(imei, begin, end)
    if cached:
        return imei, cached
    from report import find_trips
    try:
        return imei, find_trips(imei, begin, end) or []
    except Exception as exc:
        from jimi import RateLimitExceeded
        if isinstance(exc, RateLimitExceeded):
            raise
        log.warning("find_trips failed for %s: %s", imei, exc)
        return imei, []


def _fetch_many(imeis, begin, end):
    if not imeis:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix="trip-history") as pool:
        futs = {pool.submit(_fetch_one, i, begin, end): i for i in imeis}
        for f in as_completed(futs):
            imei = futs[f]
            _imei, trips = f.result()
            out[imei] = trips
    return out


def _normalise(imei: str, raw: dict, windows: list, begin: datetime, end: datetime) -> dict | None:
    t_start = _coerce_dt(raw.get("start"))
    t_end   = _coerce_dt(raw.get("end"))
    if t_start is None or t_end is None:
        return None
    if t_end <= t_start:
        return None
    if not (begin <= t_end < end):
        return None

    # First try attribution at trip start (the correct moment)
    did = _driver_at(windows, t_start)
    tid = _truck_at(windows, t_start)
    attribution = "trip_start" if did is not None else "none"

    # If no driver found at trip start, fallback to trip end
    if did is None:
        did = _driver_at(windows, t_end)
        tid = _truck_at(windows, t_end)
        if did is not None:
            attribution = "trip_end_fallback"

    duration_s = int((t_end - t_start).total_seconds())
    return {
        "imei":       imei,
        "truck_id":   tid or "",
        "driver_id":  did,
        "start":      t_start,
        "end":        t_end,
        "duration_s": duration_s,
        "distance":   raw.get("distance") or raw.get("mileage") or None,
        "raw":        raw,
        "attribution": attribution,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def trips_by_day(day_utc: datetime) -> list[dict]:
    if day_utc.tzinfo is None:
        day_utc = day_utc.replace(tzinfo=timezone.utc)
    start = day_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=1)
    imeis = _imeis_for_range(start, end)[:MAX_IMEIS_PER_QUERY]
    return _finalise(_fetch_many(imeis, start, end), start, end)


def trips_by_range(begin: datetime, end: datetime, imeis: list[str] | None = None) -> list[dict]:
    if begin.tzinfo is None:
        begin = begin.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= begin:
        return []
    targets = imeis if imeis is not None else _imeis_for_range(begin, end)
    targets = [t for t in targets if t][:MAX_IMEIS_PER_QUERY]
    return _finalise(_fetch_many(targets, begin, end), begin, end)


def trips_by_driver(driver_ids, begin: datetime, end: datetime) -> list[dict]:
    if isinstance(driver_ids, str):
        driver_ids = [driver_ids]
    if begin.tzinfo is None: begin = begin.replace(tzinfo=timezone.utc)
    if end.tzinfo is None: end = end.replace(tzinfo=timezone.utc)
    imeis = _imeis_for_driver(driver_ids, begin, end)
    if not imeis:
        return []
    trips_by_imei = _fetch_many(imeis, begin, end)
    rows = _normalise_all(trips_by_imei, begin, end)
    ids = set(driver_ids)
    rows = [r for r in rows if r["driver_id"] in ids]
    return _decorate(rows)


def _normalise_all(trips_by_imei, begin, end):
    rows = []
    for imei, trips in trips_by_imei.items():
        windows = _assignment_windows(imei, begin, end)
        for raw in trips:
            t = _normalise(imei, raw, windows, begin, end)
            if t is None:
                continue
            if t["driver_id"] is None:
                log.info("trip_history: unattributed trip imei=%s truck=%s start=%s end=%s",
                         imei, t["truck_id"], t["start"], t["end"])
            rows.append(t)
            _cache_trip(t)
    return rows


def _finalise(trips_by_imei, begin, end):
    return _decorate(_normalise_all(trips_by_imei, begin, end))


def _decorate(rows):
    names   = _driver_name_map([r["driver_id"] for r in rows if r["driver_id"]])
    truck_n = _truck_name_map([r["imei"] for r in rows])
    for r in rows:
        r["driver_name"] = names.get(r["driver_id"] or "", "")
        if not r["truck_id"]:
            r["truck_id"] = truck_n.get(r["imei"], "")
    rows.sort(key=lambda r: r["end"], reverse=True)
    return rows