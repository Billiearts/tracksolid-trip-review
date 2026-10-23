"""
report.py — trip detection, event summarisation, and Excel template filling.

Stop detection merges two sources:
  - Track points (jimi.device.track.list) — catches stops the parking API
    misses, e.g. deep-sleep overnight stops reported with a single sample.
  - Parking events (jimi.open.platform.report.parking) — accurate boundaries
    and an address when it fires.

Both sources filter their own stops at >= MIN_STOP_SECONDS before merging.
Track detection uses raw run durations only (no extension), which prevents
the historical over-extension bug where a 3-4 minute stop was inflated past
the 5-minute threshold.

Yard operations:
  - A yard visit is the gap between two consecutive trips of the same truck,
    i.e. from when the truck exits the trip geofence (TRIP TIME GBC / GBC
    TRIP TIME) to when it enters it again for the next trip. When both
    GBC EXIT / GBC EXITED bracket events are present in the alarm stream
    they take precedence, but they are optional — the alarm list only emits
    'out' events for GBC EXITED, so the trip-time fence is the reliable
    anchor.
  - Inside the bracket we walk the ordered stations MAINTENANCE TIME →
    WAYBILL TIME → FUELING TIME → LOADING TIME → WEIGHING TIME → WAITING TIME,
    taking the union of each station's own in/out intervals clipped to the
    visit window. Wrapper fences (FUEL AND WAYBILL, LOADED AND PARKED,
    HAULAGE TRUCKS EXIT, GBC AWASO, GBC AWASO 2) and stayTimeIn/stayTimeOut
    alerts never contribute to a station total.
  - For a selected trip, "before trip" yard = the last yard visit whose exit
    is at or before the trip start.

Pairing robustness (fix for phantom Waybill/Maintenance hours):
  Tracksolid does not always emit a balanced in/out stream for a fence —
  e.g. WAYBILL TIME ships ~551 'in' but only ~472 'out' across the fleet.
  Any naive "open on in, close on out" pairing therefore leaks a stale open
  'in' forward and pairs it with a much later 'out', producing multi-hour
  phantom intervals that survive clipping to the yard visit. To fix that:
    1. Station boundaries are keyed by (geoid, event) so same-named fences
       on different devices/regions never cross-pair.
    2. When a new 'in' arrives while an interval is already open, the stale
       open 'in' is discarded instead of being kept alive.
    3. Leftover open 'in's at the end of the stream are discarded.
    4. A guardrail drops any single station interval longer than the yard
       visit itself — those are definitionally bogus.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl import load_workbook

from jimi import (
    api_call, fetch_alarms, fetch_alarms_chunked,
    fetch_parking, RateLimitExceeded,
)


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The alarm stream carries two different geofences for the same concept:
#   "TRIP TIME GBC"  → geoid 791a3bb6...  (older devices)
#   "GBC TRIP TIME"  → geoid 6c5a0541...  (newer devices)
# Both mean "the truck is on a trip"; the truck exits either to enter the
# yard and re-enters either to start the next trip.
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
MERGE_GAP_SECONDS    = 10

FMT = "%Y-%m-%d %H:%M:%S"

# Excel number format for durations — [h] allows hours > 24.
DURATION_FORMAT = "[h]:mm:ss"

# Optional yard-visit bracket fences. The alarm stream only reliably emits
# 'out' events for these, so they cannot be used alone; when both 'in' and
# 'out' are present for the same device, they refine the trip-time anchor.
YARD_BRACKET_NAMES = ("GBC EXIT", "GBC EXITED")

# Ordered yard stations. Each pair is (key used in the computed dict, the
# fence name as embedded in `Enter geo-fence(NAME)` / `Exit geo-fence(NAME)`).
YARD_STATIONS = (
    ("maintenance", "MAINTENANCE TIME"),
    ("waybill",     "WAYBILL TIME"),
    ("fueling",     "FUELING TIME"),
    ("loading",     "LOADING TIME"),
    ("weighing",    "WEIGHING TIME"),
    ("waiting",     "WAITING TIME"),
)
_YARD_STATION_NAME_TO_KEY = {name: key for key, name in YARD_STATIONS}

# Human-readable labels for each station key (used by the Excel writer).
YARD_STATION_LABELS = {
    "maintenance": "Time at Maintenance",
    "waybill":     "Time for Waybill",
    "fueling":     "Time for Fueling",
    "loading":     "Time for Loading",
    "weighing":    "Time for weighing",
    "waiting":     "Time at Waiting (Dispatch)",
}

# Wrapper fences — they appear in the alarm list but overlap the granular
# station fences, so they must never contribute to a station total.
YARD_WRAPPER_NAMES = frozenset({
    "FUEL AND WAYBILL",
    "LOADED AND PARKED",
    "HAULAGE TRUCKS EXIT",
    "GBC AWASO",
    "GBC AWASO 2",
})

# Look-back window used to catch the yard visit that precedes a trip when
# 3-month history is disabled (so all_alarms still covers that visit).
YARD_LOOKBACK = timedelta(hours=48)

# Grace period between a yard visit's `out` event and the trip start —
# Tracksolid alarms can race by a few seconds; anything bigger than this
# would be a different visit.
YARD_TRIP_TOLERANCE = timedelta(minutes=5)


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
# Alarm helpers — fence name and boundary event extraction
# ---------------------------------------------------------------------------

_FENCE_NAME_RE = re.compile(r"\(([^()]*)\)\s*$")


def _alarm_fence_name(alarm: dict) -> str:
    """Return the fence name from `Enter geo-fence(NAME)` / `Exit geo-fence(NAME)`."""
    name = alarm.get("alarmTypeName") or ""
    m = _FENCE_NAME_RE.search(name)
    return m.group(1).strip() if m else ""


def _alarm_boundary_event(alarm: dict) -> str | None:
    """Return 'in', 'out', or None (only boundary events, not warnings)."""
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
# Trip detection (GBC geofence alarms)
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
                # glitch: out before or equal to in — discard
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
# Track fetch
# ---------------------------------------------------------------------------

def fetch_track(imei: str, begin: datetime, end: datetime) -> list[dict]:
    response = api_call("jimi.device.track.list", {
        "imei":       imei,
        "begin_time": utc_str(begin),
        "end_time":   utc_str(end),
    })
    result = response.get("result") or response.get("data") or []
    if isinstance(result, dict):
        result = result.get("list", [])
    return result or []


# ---------------------------------------------------------------------------
# Stop detection — track source
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


# ---------------------------------------------------------------------------
# Stop detection — parking source
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Merge stop intervals
# ---------------------------------------------------------------------------

def _merge_stop_intervals(parking_intervals: list[dict],
                          track_intervals: list[dict]) -> list[dict]:
    """
    Parking events are authoritative. Any track-detected interval that
    overlaps a parking interval is dropped. Any track interval that doesn't
    overlap a parking interval is added as-is.
    """
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


# ---------------------------------------------------------------------------
# Public stop summariser
# ---------------------------------------------------------------------------

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
# Yard operations — visit detection and station breakdown
# ---------------------------------------------------------------------------

def _pair_boundaries(events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime]]:
    """Pair in/out boundary events into non-overlapping (start, end) intervals.

    Robust against the Tracksolid stream shipping more 'in's than 'out's:

    - events are sorted by timestamp
    - the first 'in' opens an interval
    - a *second* 'in' while an interval is already open is a sign that the
      previous 'out' was lost — the stale open 'in' is discarded, and the
      new 'in' becomes the open start. Keeping the stale 'in' alive is the
      root cause of phantom multi-hour station totals (e.g. Waybill Time
      showing 6+ hours because a leftover waybill 'in' from a previous visit
      got paired with a later waybill 'out').
    - 'out' closes the interval; an 'out' while closed is ignored
    - 'out' at or before 'in' is discarded (glitch)
    - a leftover open 'in' at end of stream is discarded
    """
    events = sorted(events, key=lambda x: x[0])
    intervals: list[tuple[datetime, datetime]] = []
    open_start: datetime | None = None
    for t, ev in events:
        if ev == "in":
            if open_start is not None:
                # Previous 'in' never saw its 'out' — drop it rather than
                # letting it bleed into a later visit's 'out'.
                log.debug("discarding unpaired 'in' at %s", open_start)
            open_start = t
        else:  # out
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
    """Single pass over alarms: extract bracket and station boundary events.

    Returns (brackets, stations, trip_events):
      - brackets: [(t, in|out)] for GBC EXIT / GBC EXITED (optional)
      - stations: dict[key] -> list[(t, in|out)] for the granular station
        fences. Keyed only by station name; geoid is not needed here because
        the caller has already filtered to a single device's alarms, and
        Tracksolid uses one geoid per named fence. (Kept simple for backwards
        compatibility with persisted reports.)
      - trip_events: [(t, in|out)] for the trip-time fence (both geoids)
    """
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
            continue
        # wrapper fence (or anything else) — ignored on purpose
    return brackets, stations, trip_events


def _bracket_intervals(brackets: list[tuple[datetime, str]],
                       trip_events: list[tuple[datetime, str]]) -> list[tuple[datetime, datetime]]:
    """Compute yard-visit intervals.

    Primary source: gaps between consecutive trips — i.e. pair each trip
    'out' with the following trip 'in'. That matches the Tracksolid geofence
    report: the yard visit is the inter-trip gap.

    Secondary source: GBC EXIT / GBC EXITED in/out pairs when the alarm
    stream actually emitted the 'in' side (it usually doesn't). These are
    merged into the primary intervals so the bracket is never worse than
    the trip-time gaps alone.
    """
    trip_events_sorted = sorted(trip_events, key=lambda x: x[0])
    out_events = [t for t, ev in trip_events_sorted if ev == "out"]
    in_events = [t for t, ev in trip_events_sorted if ev == "in"]

    primary: list[tuple[datetime, datetime]] = []
    for out_t in out_events:
        # next trip 'in' strictly after out_t
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
    """Pick the yard visit that immediately precedes trip_start from a prep tuple.

    If trip_start is None (no trip detected), return the last visit.
    Returns None if there is no eligible visit.
    """
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

        # Guardrail: a single station interval can never legitimately be
        # longer than the yard visit that contains it. Anything longer is a
        # sign of a mis-paired boundary and must be dropped before clipping
        # (otherwise a leftover 'in' from a previous visit gets attributed
        # to this one).
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
# Event summarisation
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
    """Aggregate yard stats across multiple historical visits.

    Returns None if there are no valid visits to aggregate.
    """
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


def build_history(imei: str, reference: datetime, *,
                  alarms: list[dict] | None = None,
                  hist_begin: datetime | None = None,
                  hist_end: datetime | None = None) -> dict | None:
    if hist_begin is None or hist_end is None:
        hist_begin, hist_end = history_window(reference)
    log.info("history window %s → %s", utc_str(hist_begin), utc_str(hist_end))

    if alarms is None:
        try:
            alarms = fetch_alarms_chunked(imei, hist_begin, hist_end)
        except RateLimitExceeded as exc:
            log.warning("history skipped — rate limit hit: %s", exc)
            return None

    # Pre-parse alarm times once so filtering per trip doesn't re-parse strings.
    alarms_with_time = [(a, _alarm_time(a)) for a in alarms]
    alarms_with_time = [(a, t) for a, t in alarms_with_time if t is not None]

    gbc = _gbc_alarms_in_window(alarms, hist_begin, hist_end)
    trips = _trips_from_gbc_alarms(gbc)
    trips = [t for t in trips if hist_begin <= t["start"] < hist_end]
    log.info("%d trip(s) starting in window", len(trips))

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

    for t in trips:
        trip_alarms = [
            a for a, tt in alarms_with_time
            if t["start"] <= tt <= t["end"]
        ]

        try:
            parking = fetch_parking(imei, t["start"], t["end"])
        except RateLimitExceeded:
            parking = None
        except Exception as exc:
            log.warning("history: parking fetch failed for %s: %s",
                        t["start"], exc)
            parking = None

        try:
            track = fetch_track(imei, t["start"], t["end"])
        except RateLimitExceeded:
            track = None
        except Exception as exc:
            log.warning("history: track fetch failed for %s: %s",
                        t["start"], exc)
            track = None

        if parking is None or track is None:
            # Skip this trip from the aggregates rather than contributing
            # zeros that would drag the averages down.
            log.info("history: skipping trip %s (parking/track unavailable)",
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


# ---------------------------------------------------------------------------
# Excel filling
# ---------------------------------------------------------------------------
#
# Values are located by scanning column A for the *label text* rather than
# relying on hard-coded row numbers. This makes the fill robust against the
# template moving rows around.
#
# Column layout used by the template (sheet "Sheet1"):
#   A  : label
#   E  : "Before Trip" / "This Trip" value  (also "Hours" for durations)
#   F  : "Max speed" (driver behaviour only)
#   H  : history "Average per visit" / "Average per trip" / "Total Events"
#   I  : history "Longest / Largest" / "Max speed"
#   J  : history "Shortest / Smallest" / "Average Max"

FMT_DT = "%Y-%m-%d %H:%M:%S"


def _find_row(ws, *labels: str, max_row: int = 120) -> int | None:
    """Return the 1-based row where column A contains one of `labels`.

    Matching is case-insensitive substring based so that small template
    wording tweaks don't break the lookup.
    """
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(1, max_row + 1):
        cell = ws.cell(row=row, column=1)
        value = cell.value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


def _find_row_after(ws, start_row: int, *labels: str,
                    max_row: int = 120) -> int | None:
    """Like `_find_row` but only searches from `start_row` downward.

    Used for labels that appear multiple times (e.g. "Duration of ..."
    in both the This-Trip and history sections).
    """
    needles = [lbl.strip().lower() for lbl in labels]
    for row in range(start_row, max_row + 1):
        cell = ws.cell(row=row, column=1)
        value = cell.value
        if value is None:
            continue
        text = str(value).strip().lower()
        if not text:
            continue
        for needle in needles:
            if needle and needle in text:
                return row
    return None


def _safe_write(ws, coord_or_none, value, number_format: str | None = None) -> None:
    if coord_or_none is None:
        return
    target = coord_or_none
    for mr in ws.merged_cells.ranges:
        if coord_or_none in mr:
            anchor = str(mr).split(":")[0]
            if anchor != coord_or_none:
                log.debug("%s inside merge %s; → %s", coord_or_none, mr, anchor)
                target = anchor
            break
    ws[target] = value
    if number_format:
        ws[target].number_format = number_format


def _write_at(ws, row: int | None, column: str, value,
              number_format: str | None = None) -> None:
    if row is None:
        return
    _safe_write(ws, f"{column}{row}", value, number_format)


def _duration(seconds) -> float:
    try:
        s = float(seconds or 0)
    except (TypeError, ValueError):
        s = 0.0
    return s / 86400.0


def _blank_to_none(v):
    if v is None:
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def fill_template(template_path: Path, output_path: Path, *,
                  truck_id: str, driver_name: str,
                  trip_start: datetime | None,
                  trip_end: datetime | None,
                  stops: dict, events: dict,
                  yard: dict | None,
                  history: dict | None,
                  manual: dict | None = None) -> None:
    wb = load_workbook(template_path)
    ws = wb["Sheet1"]
    manual = manual or {}

    # ------------------------------------------------------------------
    # Header block — values land in column D next to the label.
    # ------------------------------------------------------------------
    for label, value in (
        ("Report Generated", datetime.now(timezone.utc).strftime(FMT_DT)),
        ("Trip Start Date/Time", trip_start.strftime(FMT_DT) if trip_start else None),
        ("Trip End Date/Time",   trip_end.strftime(FMT_DT) if trip_end else None),
        ("Trck ID",              truck_id),
    ):
        if value is None:
            continue
        row = _find_row(ws, label)
        _write_at(ws, row, "D", value)

    # Driver name — manual override wins.
    row = _find_row(ws, "Drivers Name", "Driver's Name", "Driver Name")
    effective_driver = _blank_to_none(manual.get("driver_name"))
    _write_at(ws, row, "D",
              effective_driver if effective_driver is not None else driver_name)

    # ------------------------------------------------------------------
    # Yard Operations (Before Trip) — column E holds the duration.
    # ------------------------------------------------------------------
    if yard is not None:
        for key, _ in YARD_STATIONS:
            label = YARD_STATION_LABELS[key]
            row = _find_row(ws, label)
            _write_at(ws, row, "E",
                      _duration(yard["operations"][key]["total_seconds"]),
                      DURATION_FORMAT)
        row = _find_row(ws, "Total Yard Time")
        _write_at(ws, row, "E",
                  _duration(yard["total_seconds"]), DURATION_FORMAT)

    # ------------------------------------------------------------------
    # Rest and Breaks (This Trip)
    # ------------------------------------------------------------------
    row = _find_row(ws, "Duration of Manditory rest break",
                    "Duration of Mandatory rest break")
    _write_at(ws, row, "E",
              _duration(stops["rest_break_seconds"]), DURATION_FORMAT)

    row = _find_row(ws, "Number of stops exceeding 5 mins")
    _write_at(ws, row, "E", stops["count"])

    row = _find_row(ws, "Duration of stops exceeding 5 mins")
    _write_at(ws, row, "E",
              _duration(stops["total_seconds"]), DURATION_FORMAT)

    # ------------------------------------------------------------------
    # Driver Behaviour (This Trip) — count in E, max speed in F (row-specific
    # rows because the labels carry note numbers).
    # ------------------------------------------------------------------
    c, m = events["counts"], events["max_speed"]

    def _behaviour_row(label_start: str) -> int | None:
        return _find_row(ws, label_start)

    r = _behaviour_row("Hard breaking")
    if r is None:
        r = _behaviour_row("Hard braking")
    _write_at(ws, r, "E", c["hard_braking"])
    _write_at(ws, r, "F", round(m["hard_braking"], 1))

    r = _behaviour_row("Hard cornering")
    _write_at(ws, r, "E", c["hard_cornering"])
    _write_at(ws, r, "F", round(m["hard_cornering"], 1))

    r = _behaviour_row("Speeding event <10kph")
    _write_at(ws, r, "E", c["speeding_10"])
    _write_at(ws, r, "F", round(m["speeding_10"], 1))

    r = _behaviour_row("Speeding event <20kph")
    _write_at(ws, r, "E", c["speeding_20"])
    _write_at(ws, r, "F", round(m["speeding_20"], 1))

    r = _behaviour_row("Speeding events >20kph")
    _write_at(ws, r, "E", c["speeding_over_20"])
    _write_at(ws, r, "F", round(m["speeding_over_20"], 1))

    # ------------------------------------------------------------------
    # History (Previous 3 months) — H/I/J columns.
    # ------------------------------------------------------------------
    if history and history["trip_count"] > 0:
        h = history

        # Yard history (same labels as the before-trip block, so re-scan
        # from row 14 downward to hit the same label positions — the
        # template uses one label per row for both columns).
        if h.get("yard"):
            yh = h["yard"]
            for key, _ in YARD_STATIONS:
                label = YARD_STATION_LABELS[key]
                row = _find_row(ws, label)
                op = yh["operations"][key]
                _write_at(ws, row, "H", _duration(op["avg_seconds"]), DURATION_FORMAT)
                _write_at(ws, row, "I", _duration(op["max_seconds"]), DURATION_FORMAT)
                _write_at(ws, row, "J", _duration(op["min_seconds"]), DURATION_FORMAT)

            row = _find_row(ws, "Total Yard Time")
            _write_at(ws, row, "H",
                      _duration(yh["avg_total_seconds"]), DURATION_FORMAT)
            _write_at(ws, row, "I",
                      _duration(yh["max_total_seconds"]), DURATION_FORMAT)
            _write_at(ws, row, "J",
                      _duration(yh["min_total_seconds"]), DURATION_FORMAT)

        # Rest and Breaks history — the same label rows as the this-trip
        # block; values go into H/I/J.
        row = _find_row(ws, "Duration of Manditory rest break",
                        "Duration of Mandatory rest break")
        _write_at(ws, row, "H",
                  _duration(h["rests"]["avg_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "I",
                  _duration(h["rests"]["max_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "J",
                  _duration(h["rests"]["min_seconds"]), DURATION_FORMAT)

        row = _find_row(ws, "Number of stops exceeding 5 mins")
        _write_at(ws, row, "H", round(h["stops"]["avg_count"], 1))
        _write_at(ws, row, "I", h["stops"]["max_count"])
        _write_at(ws, row, "J", h["stops"]["min_count"])

        row = _find_row(ws, "Duration of stops exceeding 5 mins")
        _write_at(ws, row, "H",
                  _duration(h["stops"]["avg_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "I",
                  _duration(h["stops"]["max_seconds"]), DURATION_FORMAT)
        _write_at(ws, row, "J",
                  _duration(h["stops"]["min_seconds"]), DURATION_FORMAT)

        # Behaviour history — Total events / Max speed / Average Max.
        b = h["behaviours"]

        r = _behaviour_row("Hard breaking")
        if r is None:
            r = _behaviour_row("Hard braking")
        _write_at(ws, r, "H", b["hard_braking"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_braking"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_braking"]["avg_max_speed"], 1))

        r = _behaviour_row("Hard cornering")
        _write_at(ws, r, "H", b["hard_cornering"]["total_events"])
        _write_at(ws, r, "I", round(b["hard_cornering"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["hard_cornering"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding event <10kph")
        _write_at(ws, r, "H", b["speeding_10"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_10"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_10"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding event <20kph")
        _write_at(ws, r, "H", b["speeding_20"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_20"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_20"]["avg_max_speed"], 1))

        r = _behaviour_row("Speeding events >20kph")
        _write_at(ws, r, "H", b["speeding_over_20"]["total_events"])
        _write_at(ws, r, "I", round(b["speeding_over_20"]["max_speed"], 1))
        _write_at(ws, r, "J", round(b["speeding_over_20"]["avg_max_speed"], 1))

    # ------------------------------------------------------------------
    # Manual fields (Statistics + signature)
    # ------------------------------------------------------------------
    for label, key in (
        ("Out of fuel event", "out_of_fuel"),
        ("Accidents",         "accidents"),
        ("Tyres damaged",     "tyres_damaged"),
        ("Breakdown",         "breakdown"),
    ):
        val = _blank_to_none(manual.get(key))
        if val is None:
            continue
        row = _find_row(ws, label)
        _write_at(ws, row, "E", val)

    signature = _blank_to_none(manual.get("signature"))
    if signature is not None:
        row = _find_row(ws, "Drivers Signature", "Driver's Signature")
        _write_at(ws, row, "D", signature)

    wb.save(output_path)


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------

def compute_report(*, imei: str, truck_id: str, driver_name: str,
                   begin: datetime, end: datetime,
                   include_history: bool = True) -> dict:
    if include_history:
        hist_begin, hist_end = history_window(begin)
        union_begin = min(begin, hist_begin)
        union_end   = max(end,   hist_end)
    else:
        hist_begin = hist_end = None
        # Even without history, look back a bit so we can catch the yard
        # visit that precedes this trip (it is by definition before begin).
        union_begin = begin - YARD_LOOKBACK
        union_end   = end

    log.info("[1/6] Fetching alarms for %s → %s ...",
             utc_str(union_begin), utc_str(union_end))
    try:
        all_alarms = fetch_alarms_chunked(imei, union_begin, union_end)
    except RateLimitExceeded:
        raise
    log.info("      %d alarms fetched", len(all_alarms))

    gbc = _gbc_alarms_in_window(all_alarms, begin, end)
    trips = _trips_from_gbc_alarms(gbc)
    log.info("      %d trip(s) in search window", len(trips))

    if trips:
        trip = trips[0]
        trip_start, trip_end = trip["start"], trip["end"]
    else:
        trip_start = trip_end = None

    log.info("[2/6] Fetching track ...")
    try:
        track = fetch_track(imei, trip_start or begin,
                            trip_end or end) if trip_start else []
    except RateLimitExceeded:
        track = []
    log.info("      %d track points", len(track))

    log.info("[3/6] Fetching parking events ...")
    try:
        parking = fetch_parking(imei, trip_start or begin,
                                trip_end or end) if trip_start else []
    except RateLimitExceeded:
        parking = []
    log.info("      %d parking events", len(parking))

    log.info("[4/6] Filtering alarms for trip window ...")
    if trip_start and trip_end:
        alarms = [
            a for a in all_alarms
            if (tt := _alarm_time(a)) is not None
            and trip_start <= tt <= trip_end
        ]
    else:
        alarms = []
    log.info("      %d alarms in trip", len(alarms))

    log.info("[5/6] Summarising ...")
    stops  = summarise_stops(track, parking, trip_start, trip_end, debug=False)
    events = summarise_events(alarms)
    yard   = summarise_yard(all_alarms, trip_start)
    log.info("      stops >= %dm: %d  total %.2fh  (rests >= %dh: %d)",
             MIN_STOP_SECONDS // 60, stops["count"],
             stops["total_seconds"] / 3600,
             MIN_REST_SECONDS // 3600, stops["rest_break_count"])
    if yard:
        log.info("      yard visit %s → %s  (total %.2fh)",
                 utc_str(yard["visit"]["start"]),
                 utc_str(yard["visit"]["end"]),
                 yard["total_seconds"] / 3600)
        for key, _ in YARD_STATIONS:
            log.info("        %-12s %.2fh", key,
                     yard["operations"][key]["total_seconds"] / 3600)
    else:
        log.info("      yard visit: none before this trip")

    history = None
    if include_history and trip_start:
        log.info("[6/6] Building 3-month history (reusing fetched alarms) ...")
        try:
            history = build_history(
                imei, trip_start,
                alarms=all_alarms,
                hist_begin=hist_begin,
                hist_end=hist_end,
            )
        except RateLimitExceeded as exc:
            log.warning("history skipped (rate limit): %s", exc)
            history = None
        except Exception as exc:
            log.warning("history failed: %s", exc)

    return {
        "truck_id":      truck_id,
        "driver_name":   driver_name,
        "imei":          imei,
        "query_begin":   begin,
        "query_end":     end,
        "trip_start":    trip_start,
        "trip_end":      trip_end,
        "stops":         stops,
        "events":        events,
        "yard":          yard,
        "history":       history,
        "trips":         trips,
        "alarm_count":   len(alarms),
        "parking_count": len(parking),
        "track_count":   len(track),
        "generated_at":  datetime.now(timezone.utc),
    }


def write_excel(computed: dict, template_path: Path, output_path: Path,
                *,
                created_by: str | None = None,
                created_by_email: str | None = None,
                manual: dict | None = None) -> None:
    log.info("[write] Writing %s ...", output_path)
    fill_template(
        template_path, output_path,
        truck_id=computed["truck_id"],
        driver_name=computed["driver_name"],
        trip_start=computed["trip_start"],
        trip_end=computed["trip_end"],
        stops=computed["stops"],
        events=computed["events"],
        yard=computed.get("yard"),
        history=computed["history"],
        manual=manual,
    )

    ts = computed["trip_start"]
    te = computed["trip_end"]
    stops = computed["stops"]
    history = computed["history"]
    yard = computed.get("yard")
    meta = {
        "truck_id":         computed["truck_id"],
        "driver_name":      computed["driver_name"],
        "imei":             computed["imei"],
        "trip_start":       utc_str(ts) if ts else None,
        "trip_end":         utc_str(te) if te else None,
        "query_begin":      utc_str(computed["query_begin"]),
        "query_end":        utc_str(computed["query_end"]),
        "generated_at":     utc_str(computed["generated_at"]),
        "trips_found":      len(computed["trips"]),
        "alarm_count":      computed["alarm_count"],
        "parking_count":    computed.get("parking_count", 0),
        "track_count":      computed.get("track_count", 0),
        "stops_count":      stops["count"],
        "stops_seconds":    stops["total_seconds"],
        "rest_break_count": stops["rest_break_count"],
        "rest_break_seconds": stops["rest_break_seconds"],
        "history_trips":    history["trip_count"] if history else 0,
        "yard_total_seconds": yard["total_seconds"] if yard else None,
        "yard_station_seconds": (
            {k: yard["operations"][k]["total_seconds"] for k in yard["operations"]}
            if yard else None
        ),
        "created_by":       created_by,
        "created_by_email": created_by_email,
        "manual":           manual or {},
    }
    try:
        output_path.with_suffix(".meta.json").write_text(
            json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("could not write metadata sidecar: %s", exc)


def build_report(*, imei: str, truck_id: str, driver_name: str,
                 begin: datetime, end: datetime,
                 template_path: Path, output_path: Path,
                 include_history: bool = True,
                 manual: dict | None = None) -> dict:
    computed = compute_report(
        imei=imei, truck_id=truck_id, driver_name=driver_name,
        begin=begin, end=end, include_history=include_history,
    )
    write_excel(computed, template_path, output_path, manual=manual)
    return computed