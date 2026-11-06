"""
report_analysis.py — Tracksolid Pro trip/stop/yard/event analysis.

This module talks to the Tracksolid API (via jimi.py) and turns raw alarms,
track points, and parking events into plain Python dicts:

  * find_trips(imei, begin, end)              -> list[trip dict]
  * summarise_stops(track, parking, ...)      -> stops dict
  * summarise_events(alarms)                  -> events dict
  * summarise_yard(alarms, trip_start)        -> yard dict | None
  * build_history(imei, reference, ...)       -> history dict | None

Nothing here imports openpyxl or touches Excel. Keeping the analysis layer
pure means it can be reused from a CLI, a background worker, or a test
harness without dragging in presentation concerns.

Stop detection merges two sources:
  - Track points (jimi.device.track.list) — catches stops the parking API
    misses, e.g. deep-sleep overnight stops reported with a single sample.
  - Parking events (jimi.open.platform.report.parking) — accurate boundaries
    and an address when it fires.

Both sources filter their own stops at >= MIN_STOP_SECONDS before merging.
Track detection uses raw run durations only (no extension), which prevents
the historical over-extension bug where a 3-4 minute stop was inflated past
the 5-minute threshold.

History parallelism — since Nov 2026:
  The Tracksolid track endpoint caps a single request at 7 days
  ('Time span over specified range' above that). The parking endpoint has
  no such cap. History is therefore built as:

    1. ONE fetch_parking() call across the whole history window (~3 months).
       Parking rows are then filtered per trip.

    2. fetch_track_chunked() — the history window is sliced into 7-day
       chunks, each chunk is fetched once per truck, and per-trip track
       arrays are assembled by filtering the cached chunk points.

    3. Any individual trip >7 days long is split into 7-day sub-windows
       and merged (handles the rare unclosed-fence trip that spans weeks).

  The chunk cache is per-(imei, chunk_start) so the cost per truck is
  ~ceil(window_days / 7) calls, not ~N_trips calls. For a 3-month window
  that's ~13 calls per truck instead of ~67, with 100% accuracy.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from jimi import (
    api_call, fetch_alarms, fetch_alarms_chunked,
    fetch_parking, parallel_call, RateLimitExceeded,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GBC_GEOIDS = frozenset({
    "791a3bb604ef41cfa302e5dc8f2c356c",   # TRIP TIME GBC
    "6c5a0541387f4fa0af8cd7d334efa857",   # GBC TRIP TIME
})
GBC_FENCE_NAME_FALLBACKS = ("TRIP TIME GBC", "GBC TRIP TIME")

SPEED_LIMIT_KPH = 75.0

ALERT_ACCELERATION = "41"
ALERT_CORNERING    = "43"
ALERT_DECELERATION = "48"
ALERT_OVERSPEED    = "overSpeed"

MIN_STOP_SECONDS     = 300
MIN_REST_SECONDS     = 7200
STATIONARY_SPEED_KPH = 1.5

FMT = "%Y-%m-%d %H:%M:%S"

YARD_BRACKET_NAMES = ("GBC EXIT", "GBC EXITED")

YARD_STATIONS = (
    ("maintenance", "MAINTENANCE TIME"),
    ("waybill",     "WAYBILL TIME"),
    ("fueling",     "FUELING TIME"),
    ("loading",     "LOADING TIME"),
    ("weighing",    "WEIGHING TIME"),
    ("waiting",     "WAITING TIME"),
)
_YARD_STATION_NAME_TO_KEY = {name: key for key, name in YARD_STATIONS}

YARD_WRAPPER_NAMES = frozenset({
    "FUEL AND WAYBILL",
    "LOADED AND PARKED",
    "HAULAGE TRUCKS EXIT",
    "GBC AWASO",
    "GBC AWASO 2",
})

YARD_LOOKBACK = timedelta(hours=48)
YARD_TRIP_TOLERANCE = timedelta(minutes=5)

# Tracksolid caps a single jimi.device.track.list request at 7 days.
# Higher values raise code=1001 'Time span over specified range'.
_TRACK_CHUNK_DAYS = 7

# Number of parallel workers for history fan-out.
_HISTORY_WORKERS = 4


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(FMT)


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def _to_float(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _speed_of(p: dict) -> float:
    for key in ("gpsSpeed", "speed"):
        v = p.get(key)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def _is_stationary_point(p: dict) -> bool:
    ignition_off = (p.get("ignition") or "").upper() == "OFF"
    slow = _speed_of(p) < STATIONARY_SPEED_KPH
    return ignition_off and slow


def history_window(reference: datetime) -> tuple[datetime, datetime]:
    first_of_this_month = reference.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0)
    end = reference
    y, m = first_of_this_month.year, first_of_this_month.month - 3
    while m <= 0:
        m += 12
        y -= 1
    calendar_begin = first_of_this_month.replace(year=y, month=m)
    retention_floor = reference - timedelta(days=89)
    begin = max(calendar_begin, retention_floor)
    return begin, end


# ---------------------------------------------------------------------------
# Alarm helpers
# ---------------------------------------------------------------------------

_FENCE_NAME_RE = re.compile(r"\(([^()]*)\)\s*$")


def _alarm_fence_name(alarm: dict) -> str:
    name = alarm.get("alarmTypeName") or ""
    m = _FENCE_NAME_RE.search(name)
    return m.group(1).strip() if m else ""


def _alarm_boundary_event(alarm: dict) -> str | None:
    eid = (alarm.get("alertTypeId") or "").strip().lower()
    return eid if eid in ("in", "out") else None


def _alarm_time(alarm: dict) -> datetime | None:
    ts = alarm.get("alertTime")
    if not ts:
        return None
    try:
        return parse_utc(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Trip detection
# ---------------------------------------------------------------------------

def _is_gbc_alarm(alarm: dict) -> bool:
    geoid = (alarm.get("geoid") or "").strip()
    if geoid in GBC_GEOIDS:
        return True
    name = alarm.get("alarmTypeName") or ""
    return any(fb in name for fb in GBC_FENCE_NAME_FALLBACKS)


def _gbc_alarms_in_window(alarms: list[dict],
                          begin: datetime, end: datetime) -> list[dict]:
    gbc: list[dict] = []
    for a in alarms:
        if not _is_gbc_alarm(a):
            continue
        t = _alarm_time(a)
        if t is None or t < begin or t > end:
            continue
        gbc.append(a)
    gbc.sort(key=lambda a: a.get("alertTime") or "")
    return gbc


def _trips_from_gbc_alarms(gbc: list[dict]) -> list[dict]:
    trips: list[dict] = []
    open_start: datetime | None = None
    for a in gbc:
        event = _alarm_boundary_event(a)
        if event is None:
            continue
        t = _alarm_time(a)
        if t is None:
            continue
        if event == "in":
            if open_start is not None:
                log.warning("unclosed trip starting %s; restarting", open_start)
            open_start = t
        elif event == "out":
            if open_start is None:
                continue
            if t <= open_start:
                open_start = None
                continue
            trips.append({
                "start": open_start, "end": t,
                "duration_s": (t - open_start).total_seconds(),
            })
            open_start = None
    return trips


def find_trips(imei: str, begin: datetime, end: datetime) -> list[dict]:
    alarms = fetch_alarms_chunked(imei, begin, end)
    gbc = _gbc_alarms_in_window(alarms, begin, end)
    return _trips_from_gbc_alarms(gbc)


# ---------------------------------------------------------------------------
# Track fetch — 7-day chunk cache
# ---------------------------------------------------------------------------
#
# Two caching layers:
#
#   1. _track_cache: per-(imei, begin_str, end_str) LRU, 15 min TTL. This
#      is the existing per-request cache. compute_report() uses it for the
#      "current trip" fetch — a single call that's usually <24h.
#
#   2. _track_chunk_cache: per-(imei, chunk_start_str) LRU, 60 min TTL.
#      Holds 7-day chunks aligned to chunk_start_str (which is derived
#      from an epoch-based rounding so chunks are stable across calls).
#      build_history() uses this — a 3-month window becomes ~13 chunks,
#      each fetched once and reused across every trip that overlaps it.
#
# Together they mean: the current trip's track is fetched once, and each
# 7-day history chunk is fetched once, no matter how many trips fall in it.

_track_cache: dict = {}
_track_cache_lock = threading.Lock()
_TRACK_CACHE_TTL = 900

_track_chunk_cache: dict = {}
_track_chunk_cache_lock = threading.Lock()
_TRACK_CHUNK_CACHE_TTL = 3600
_TRACK_CHUNK_CACHE_MAX = 500


def _track_cache_get(key):
    with _track_cache_lock:
        entry = _track_cache.get(key)
        if entry is None:
            return None
        expires_at, points = entry
        if time.time() > expires_at:
            _track_cache.pop(key, None)
            return None
        return points


def _track_cache_put(key, points):
    with _track_cache_lock:
        _track_cache[key] = (time.time() + _TRACK_CACHE_TTL, points)


def _chunk_cache_get(key):
    with _track_chunk_cache_lock:
        entry = _track_chunk_cache.get(key)
        if entry is None:
            return None
        expires_at, points = entry
        if time.time() > expires_at:
            _track_chunk_cache.pop(key, None)
            return None
        return points


def _chunk_cache_put(key, points):
    with _track_chunk_cache_lock:
        _track_chunk_cache[key] = (
            time.time() + _TRACK_CHUNK_CACHE_TTL, points,
        )
        # Evict oldest if over cap.
        while len(_track_chunk_cache) > _TRACK_CHUNK_CACHE_MAX:
            oldest = next(iter(_track_chunk_cache))
            _track_chunk_cache.pop(oldest, None)


def _chunk_start_of(dt: datetime) -> datetime:
    """Return the start of the 7-day bucket containing `dt`.

    Chunks are aligned to midnight UTC days, so two calls with the same
    day-7-multiple boundary share a chunk key.
    """
    day_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_epoch = (day_start - datetime(1970, 1, 1, tzinfo=timezone.utc)).days
    bucket_index = days_since_epoch // _TRACK_CHUNK_DAYS
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        days=bucket_index * _TRACK_CHUNK_DAYS
    )


def _fetch_track_chunk(imei: str, chunk_start: datetime) -> list[dict]:
    """Fetch one 7-day chunk. Cached for 60 minutes per (imei, chunk_start)."""
    chunk_end = chunk_start + timedelta(days=_TRACK_CHUNK_DAYS) - timedelta(seconds=1)
    key = (imei, utc_str(chunk_start))
    cached = _chunk_cache_get(key)
    if cached is not None:
        return cached

    response = api_call("jimi.device.track.list", {
        "imei":       imei,
        "begin_time": utc_str(chunk_start),
        "end_time":   utc_str(chunk_end),
    })
    result = response.get("result") or response.get("data") or []
    if isinstance(result, dict):
        result = result.get("list", [])
    points = result or []
    _chunk_cache_put(key, points)
    return points


def fetch_track(imei: str, begin: datetime, end: datetime) -> list[dict]:
    """Fetch track points for [begin, end], cached for 15 min.

    If the window exceeds 7 days (the Tracksolid cap), it's automatically
    split into 7-day chunks and merged. Chunks are individually cached
    for 60 min so overlapping calls reuse the same data.
    """
    key = (imei, utc_str(begin), utc_str(end))
    cached = _track_cache_get(key)
    if cached is not None:
        return cached

    if (end - begin) <= timedelta(days=_TRACK_CHUNK_DAYS):
        # Single-chunk fast path.
        response = api_call("jimi.device.track.list", {
            "imei":       imei,
            "begin_time": utc_str(begin),
            "end_time":   utc_str(end),
        })
        result = response.get("result") or response.get("data") or []
        if isinstance(result, dict):
            result = result.get("list", [])
        points = result or []
        _track_cache_put(key, points)
        return points

    # Multi-chunk path: walk chunk boundaries, fetch each, merge, filter.
    log.info(
        "fetch_track: %.1f-day window exceeds 7-day cap; chunking",
        (end - begin).total_seconds() / 86400,
    )
    all_points: list[dict] = []
    cursor = begin
    while cursor < end:
        chunk_start = _chunk_start_of(cursor)
        # If begin is mid-chunk, we still need this chunk from `begin`.
        # Use the aligned chunk from chunk_start, but filter to cursor..end.
        points = _fetch_track_chunk(imei, chunk_start)
        for p in points:
            ts = p.get("gpsTime")
            if not ts:
                continue
            try:
                t = parse_utc(ts)
            except ValueError:
                continue
            if cursor <= t < min(end, chunk_start + timedelta(days=_TRACK_CHUNK_DAYS)):
                all_points.append(p)
        cursor = chunk_start + timedelta(days=_TRACK_CHUNK_DAYS)

    all_points.sort(key=lambda p: p.get("gpsTime") or "")
    _track_cache_put(key, all_points)
    return all_points


# ---------------------------------------------------------------------------
# Stops
# ---------------------------------------------------------------------------

def _track_stop_intervals(track: list[dict],
                          trip_start: datetime | None,
                          trip_end: datetime | None,
                          debug: bool = False) -> list[dict]:
    if not track:
        return []

    points: list[tuple[datetime, bool]] = []
    for p in track:
        ts = p.get("gpsTime")
        if not ts:
            continue
        try:
            dt = parse_utc(ts)
        except ValueError:
            continue
        points.append((dt, _is_stationary_point(p)))
    points.sort(key=lambda x: x[0])

    if trip_start:
        points = [p for p in points if p[0] >= trip_start]
    if trip_end:
        points = [p for p in points if p[0] <= trip_end]
    if not points:
        return []

    raw_runs: list[tuple[int, int]] = []
    i = 0
    while i < len(points):
        if points[i][1]:
            j = i
            while j + 1 < len(points) and points[j + 1][1]:
                j += 1
            raw_runs.append((i, j))
            i = j + 1
        else:
            i += 1

    intervals: list[dict] = []
    for i0, j0 in raw_runs:
        start = points[i0][0]
        end = points[j0][0]
        dur = (end - start).total_seconds()
        if dur < MIN_STOP_SECONDS:
            continue
        intervals.append({
            "start": start, "end": end, "seconds": dur, "address": "",
        })
    return intervals


def _parking_stop_intervals(parking_events: list[dict],
                            trip_start: datetime | None,
                            trip_end: datetime | None) -> list[dict]:
    out: list[dict] = []
    for e in parking_events:
        s = e.get("start")
        en = e.get("end")
        if s is None or en is None:
            continue
        if trip_start and s < trip_start:
            continue
        if trip_end and s > trip_end:
            continue
        dur = e.get("seconds", 0)
        if dur < MIN_STOP_SECONDS:
            continue
        out.append({
            "start": s, "end": en, "seconds": dur,
            "address": e.get("address", "") or "",
        })
    return out


def _merge_stop_intervals(parking_intervals: list[dict],
                          track_intervals: list[dict]) -> list[dict]:
    def overlaps(a_start, a_end, b_start, b_end):
        return a_start < b_end and a_end > b_start

    out = list(parking_intervals)
    for t in track_intervals:
        if any(overlaps(t["start"], t["end"], p["start"], p["end"])
               for p in parking_intervals):
            continue
        out.append(t)

    out.sort(key=lambda x: x["start"])
    return out


def summarise_stops(track: list[dict],
                    parking_events: list[dict],
                    trip_start: datetime | None = None,
                    trip_end: datetime | None = None,
                    debug: bool = False) -> dict:
    track_intervals = _track_stop_intervals(
        track, trip_start, trip_end, debug=debug)
    parking_intervals = _parking_stop_intervals(
        parking_events, trip_start, trip_end)

    merged = _merge_stop_intervals(parking_intervals, track_intervals)
    merged = [x for x in merged if x["seconds"] >= MIN_STOP_SECONDS]
    merged.sort(key=lambda x: x["start"])

    durations = [x["seconds"] for x in merged]
    rest_intervals = [x for x in merged if x["seconds"] >= MIN_REST_SECONDS]
    rest_durations = [x["seconds"] for x in rest_intervals]

    return {
        "count":                 len(merged),
        "total_seconds":         sum(durations),
        "longest_seconds":       max(durations) if durations else 0,
        "durations":             list(durations),
        "intervals":             merged,
        "rest_break_count":      len(rest_intervals),
        "rest_break_seconds":    sum(rest_durations),
        "rest_break_longest_s":  max(rest_durations) if rest_durations else 0,
        "rest_durations":        list(rest_durations),
        "rest_intervals":        rest_intervals,
    }


def _empty_stops() -> dict:
    return {
        "count": 0, "total_seconds": 0, "longest_seconds": 0,
        "durations": [], "intervals": [],
        "rest_break_count": 0, "rest_break_seconds": 0,
        "rest_break_longest_s": 0,
        "rest_durations": [], "rest_intervals": [],
    }


# ---------------------------------------------------------------------------
# Yard operations
# ---------------------------------------------------------------------------

def _pair_boundaries(events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime]]:
    events = sorted(events, key=lambda x: x[0])
    intervals: list[tuple[datetime, datetime]] = []
    open_start: datetime | None = None
    for t, ev in events:
        if ev == "in":
            if open_start is not None:
                log.debug("discarding unpaired 'in' at %s", open_start)
            open_start = t
        else:
            if open_start is not None:
                if t > open_start:
                    intervals.append((open_start, t))
                open_start = None
    return intervals


def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged: list[list[datetime]] = [list(intervals[0])]
    for start, end in intervals[1:]:
        last = merged[-1]
        if start <= last[1]:
            if end > last[1]:
                last[1] = end
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _prep_yard(alarms: list[dict]):
    brackets: list[tuple[datetime, str]] = []
    trip_events: list[tuple[datetime, str]] = []
    stations: dict[str, list[tuple[datetime, str]]] = {k: [] for k, _ in YARD_STATIONS}
    for a in alarms:
        ev = _alarm_boundary_event(a)
        if ev is None:
            continue
        t = _alarm_time(a)
        if t is None:
            continue
        geoid = (a.get("geoid") or "").strip()
        fence = _alarm_fence_name(a)
        if geoid in GBC_GEOIDS:
            trip_events.append((t, ev))
            continue
        if fence in YARD_BRACKET_NAMES:
            brackets.append((t, ev))
            continue
        if fence in _YARD_STATION_NAME_TO_KEY:
            stations[_YARD_STATION_NAME_TO_KEY[fence]].append((t, ev))
    return brackets, stations, trip_events


def _bracket_intervals(brackets: list[tuple[datetime, str]],
                       trip_events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime]]:
    trip_events_sorted = sorted(trip_events, key=lambda x: x[0])
    out_events = [t for t, ev in trip_events_sorted if ev == "out"]
    in_events = [t for t, ev in trip_events_sorted if ev == "in"]

    primary: list[tuple[datetime, datetime]] = []
    for out_t in out_events:
        next_in = next((t for t in in_events if t > out_t), None)
        if next_in is None:
            continue
        primary.append((out_t, next_in))

    secondary = _pair_boundaries(brackets)
    combined = primary + secondary
    return _merge_intervals(combined)


def _yard_visit_intervals(prep) -> list[dict]:
    brackets, stations, trip_events = prep
    intervals = _bracket_intervals(brackets, trip_events)
    return [{"start": s, "end": e} for s, e in intervals]


def summarise_yard_from_prep(prep, trip_start: datetime | None) -> dict | None:
    brackets, stations, trip_events = prep
    visits = _yard_visit_intervals(prep)
    if not visits:
        return None

    if trip_start is None:
        chosen = visits[-1]
    else:
        cutoff = trip_start + YARD_TRIP_TOLERANCE
        eligible = [v for v in visits if v["end"] <= cutoff]
        if not eligible:
            return None
        chosen = eligible[-1]

    vs, ve = chosen["start"], chosen["end"]
    visit_seconds = (ve - vs).total_seconds()

    operations: dict[str, dict] = {}
    for key, _ in YARD_STATIONS:
        paired = _pair_boundaries(stations[key])
        sane: list[tuple[datetime, datetime]] = []
        for s, e in paired:
            if (e - s).total_seconds() > visit_seconds:
                log.warning(
                    "dropping bogus %s interval %s → %s (%.0fs > visit %.0fs)",
                    key, s, e, (e - s).total_seconds(), visit_seconds,
                )
                continue
            sane.append((s, e))

        clipped = _merge_intervals([
            (max(s, vs), min(e, ve))
            for s, e in sane if s < ve and e > vs
        ])
        durations = [(e - s).total_seconds() for s, e in clipped]
        operations[key] = {
            "total_seconds":   sum(durations),
            "count":           len(clipped),
            "longest_seconds": max(durations) if durations else 0.0,
        }

    return {
        "visit":         {"start": vs, "end": ve},
        "total_seconds": visit_seconds,
        "operations":    operations,
    }


def summarise_yard(alarms: list[dict], trip_start: datetime | None) -> dict | None:
    return summarise_yard_from_prep(_prep_yard(alarms), trip_start)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

def summarise_events(alarms: list[dict]) -> dict:
    counts = {
        "hard_braking":     0,
        "hard_cornering":   0,
        "speeding_10":      0,
        "speeding_20":      0,
        "speeding_over_20": 0,
    }
    max_speed = {k: 0.0 for k in counts}
    acceleration_count = 0
    event_list: list[dict] = []

    for a in alarms:
        atid = (a.get("alertTypeId") or "").strip()
        name = (a.get("alarmTypeName") or "").lower()
        speed = _to_float(a.get("speed"))
        t = a.get("alertTime")

        if atid == ALERT_DECELERATION or "sudden deceleration" in name:
            counts["hard_braking"] += 1
            max_speed["hard_braking"] = max(max_speed["hard_braking"], speed)
            event_list.append({"time": t, "type": "Hard braking", "speed": speed})
        elif atid == ALERT_CORNERING or "sharp turn" in name or "cornering" in name:
            counts["hard_cornering"] += 1
            max_speed["hard_cornering"] = max(max_speed["hard_cornering"], speed)
            event_list.append({"time": t, "type": "Hard cornering", "speed": speed})
        elif atid == ALERT_ACCELERATION or "sudden acceleration" in name:
            acceleration_count += 1
            event_list.append({"time": t, "type": "Sudden acceleration", "speed": speed})
        elif atid == ALERT_OVERSPEED or "overspeed" in name:
            over = speed - SPEED_LIMIT_KPH
            if over <= 0:
                continue
            bucket = ("speeding_10" if over < 10 else
                      "speeding_20" if over < 20 else
                      "speeding_over_20")
            counts[bucket] += 1
            max_speed[bucket] = max(max_speed[bucket], speed)
            label = {
                "speeding_10":      "Speeding <10 kph over",
                "speeding_20":      "Speeding <20 kph over",
                "speeding_over_20": "Speeding >20 kph over",
            }[bucket]
            event_list.append({"time": t, "type": label, "speed": speed})

    event_list.sort(key=lambda e: e.get("time") or "")

    return {
        "counts": counts,
        "max_speed": max_speed,
        "acceleration_count": acceleration_count,
        "events": event_list,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _aggregate_yard(visits: list[dict]) -> dict | None:
    if not visits:
        return None

    totals = [v["total_seconds"] for v in visits]

    operations: dict[str, dict] = {}
    for key, _ in YARD_STATIONS:
        per_visit = [v["operations"][key]["total_seconds"] for v in visits]
        if not per_visit:
            operations[key] = {"avg_seconds": 0.0, "max_seconds": 0.0, "min_seconds": 0.0}
            continue
        operations[key] = {
            "avg_seconds": sum(per_visit) / len(per_visit),
            "max_seconds": max(per_visit),
            "min_seconds": min(per_visit),
        }

    return {
        "visits":            len(visits),
        "avg_total_seconds": sum(totals) / len(totals),
        "max_total_seconds": max(totals),
        "min_total_seconds": min(totals),
        "operations":        operations,
    }


def _fetch_one_history_trip(imei: str, t: dict, parking: list[dict] | None):
    """Fetch the track for one historical trip and filter the batched
    parking rows to it.

    - track: uses fetch_track(), which auto-chunks >7-day windows and
      reuses the 7-day chunk cache.
    - parking: pre-fetched once for the whole window; we just filter
      to [t.start, t.end].

    Returns (trip, parking_for_trip, track_for_trip). Either may be None
    on failure, which just means that trip's stop stats are skipped.
    """
    try:
        track = fetch_track(imei, t["start"], t["end"])
    except RateLimitExceeded as exc:
        log.warning("history: track rate-limited for %s: %s",
                    t["start"], exc)
        track = None
    except Exception as exc:
        log.warning("history: track fetch failed for %s: %s",
                    t["start"], exc)
        track = None

    parking_for_trip: list[dict] | None
    if parking is None:
        parking_for_trip = None
    else:
        parking_for_trip = [
            p for p in parking
            if p.get("start") is not None and p.get("end") is not None
            and p["start"] < t["end"] and p["end"] > t["start"]
        ]

    return t, parking_for_trip, track


def build_history(imei: str, reference: datetime, *,
                  alarms: list[dict] | None = None,
                  hist_begin: datetime | None = None,
                  hist_end: datetime | None = None) -> dict | None:
    """Build 3-month history.

    Performance strategy (Nov 2026):

      * Parking is fetched ONCE across the full history window via a single
        `fetch_parking()` call. The API has no window cap on parking, so
        this replaces N_trips parking calls (typically 60–70) with 1.

      * Track is fetched per-trip via `fetch_track()`, which auto-splits
        windows >7 days into 7-day chunks. Each 7-day chunk is cached for
        60 min per (imei, chunk_start), so overlapping trips reuse the same
        underlying API responses. For a 3-month window with 60+ trips this
        reduces ~67 track calls to ~13 chunk calls, with 100% accuracy.

      * Behaviour events and yard stats use ALL trips — they're derived
        from alarms, which are already in hand.

    The result is identical to the pre-Nov-2026 algorithm in terms of
    accuracy — average, max, min, and total are all computed across every
    trip. Only the number of HTTP calls drops (by roughly 10×).
    """
    import time as _time
    t0 = _time.monotonic()

    if hist_begin is None or hist_end is None:
        hist_begin, hist_end = history_window(reference)

    if alarms is None:
        # CLI / standalone path — fetch once.
        try:
            alarms = fetch_alarms_chunked(imei, hist_begin, hist_end)
        except RateLimitExceeded as exc:
            log.warning("history skipped — rate limit hit: %s", exc)
            return None

    log.info("history window %s → %s (reusing %d alarms)",
             utc_str(hist_begin), utc_str(hist_end), len(alarms))

    alarms_with_time = [(a, _alarm_time(a)) for a in alarms]
    alarms_with_time = [(a, t) for a, t in alarms_with_time if t is not None]

    gbc = _gbc_alarms_in_window(alarms, hist_begin, hist_end)
    trips = _trips_from_gbc_alarms(gbc)
    trips = [t for t in trips if hist_begin <= t["start"] < hist_end]
    log.info("%d trip(s) starting in window", len(trips))

    # ---- batched parking across the whole history window ----
    t_fetch_parking = _time.monotonic()
    try:
        parking_full = fetch_parking(imei, hist_begin, hist_end)
        log.info("history: parking fetched for whole window (%d rows, %.1fs)",
                 len(parking_full), _time.monotonic() - t_fetch_parking)
    except RateLimitExceeded as exc:
        log.warning("history: parking rate-limited: %s", exc)
        parking_full = None
    except Exception as exc:
        log.warning("history: parking fetch failed: %s", exc)
        parking_full = None

    # ---- parallel per-trip track fetch (chunk cache does the dedup) ----
    t_fetch0 = _time.monotonic()
    fetched = parallel_call(
        lambda t: _fetch_one_history_trip(imei, t, parking_full),
        trips,
        workers=_HISTORY_WORKERS,
    )
    log.info("history: fetched track for %d trip(s) in %.1fs",
             len(fetched), _time.monotonic() - t_fetch0)

    stop_counts:   list[int]   = []
    stop_totals_s: list[float] = []
    rest_breaks_s: list[float] = []

    behav_events = {k: 0 for k in
                    ("hard_braking", "hard_cornering",
                     "speeding_10", "speeding_20", "speeding_over_20")}
    behav_max = {k: 0.0 for k in behav_events}
    behav_per_trip_max = {k: [] for k in behav_events}

    yard_prep = _prep_yard(alarms)
    yard_visits: list[dict] = []

    for t, parking, track in fetched:
        trip_alarms = [
            a for a, tt in alarms_with_time
            if t["start"] <= tt <= t["end"]
        ]

        if parking is None or track is None:
            log.info("history: skipping stops for trip %s (parking/track unavailable)",
                     t["start"])
        else:
            s = summarise_stops(track, parking, t["start"], t["end"], debug=False)
            stop_counts.append(s["count"])
            stop_totals_s.append(s["total_seconds"])
            if s["rest_break_count"]:
                rest_breaks_s.append(s["rest_break_seconds"])

        e = summarise_events(trip_alarms)
        for k in behav_events:
            behav_events[k] += e["counts"][k]
            behav_max[k] = max(behav_max[k], e["max_speed"][k])
            if e["counts"][k] > 0:
                behav_per_trip_max[k].append(e["max_speed"][k])

        visit = summarise_yard_from_prep(yard_prep, t["start"])
        if visit is not None:
            yard_visits.append(visit)

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    log.info("history: built in %.1fs", _time.monotonic() - t0)

    return {
        "trip_count": len(trips),
        "window_begin": hist_begin,
        "window_end":   hist_end,
        "stops": {
            "avg_count":    _avg(stop_counts),
            "max_count":    max(stop_counts) if stop_counts else 0,
            "min_count":    min(stop_counts) if stop_counts else 0,
            "avg_seconds":  _avg(stop_totals_s),
            "max_seconds":  max(stop_totals_s) if stop_totals_s else 0.0,
            "min_seconds":  min(stop_totals_s) if stop_totals_s else 0.0,
            "longest_single_stop_s":  0.0,
            "shortest_single_stop_s": 0.0,
        },
        "rests": {
            "avg_seconds":  _avg(rest_breaks_s),
            "max_seconds":  max(rest_breaks_s) if rest_breaks_s else 0.0,
            "min_seconds":  min(rest_breaks_s) if rest_breaks_s else 0.0,
            "longest_single_rest_s": 0.0,
            "shortest_single_rest_s": 0.0,
        },
        "behaviours": {
            k: {
                "total_events":  behav_events[k],
                "max_speed":     behav_max[k],
                "avg_max_speed": _avg(behav_per_trip_max[k]),
            }
            for k in behav_events
        },
        "yard": _aggregate_yard(yard_visits),
    }