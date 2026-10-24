"""
report.py — public pipeline for the Trip Review report.

Two concerns live in sibling modules:

  * report_analysis.py — Tracksolid API queries, trip/stop/yard/event
    summarisation, 3-month history. No Excel.
  * report_excel.py    — openpyxl template filling, signature embedding,
    single-trip and bulk (multi-sheet) workbook assembly. No API calls.

This module ties them together with `compute_report()` / `write_excel()` /
`build_report()` and re-exports the public names that `app.py` and
`run_report.py` already import:

    from report import (
        compute_report, write_excel, write_bulk_excel,
        utc_str, find_trips,
    )

...and `app.py`'s lazy `from report import fill_sheet, _sanitise_sheet_name`
inside `report_edit_meta()` continues to work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from jimi import RateLimitExceeded

# --- re-exports from report_analysis ---------------------------------------
from report_analysis import (  # noqa: F401
    # constants
    GBC_GEOIDS, MIN_STOP_SECONDS, MIN_REST_SECONDS,
    YARD_STATIONS, YARD_LOOKBACK, FMT,
    # helpers
    utc_str, parse_utc, history_window,
    # public entry points
    find_trips, fetch_track, summarise_stops,
    summarise_yard, summarise_events, build_history,
)

# --- re-exports from report_excel ------------------------------------------
from report_excel import (  # noqa: F401
    fill_sheet, fill_template, write_bulk_excel, _sanitise_sheet_name,
)


log = logging.getLogger(__name__)


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
        union_begin = begin - YARD_LOOKBACK
        union_end   = end

    log.info("[1/6] Fetching alarms for %s → %s ...",
             utc_str(union_begin), utc_str(union_end))
    try:
        from jimi import fetch_alarms_chunked
        all_alarms = fetch_alarms_chunked(imei, union_begin, union_end)
    except RateLimitExceeded:
        raise
    log.info("      %d alarms fetched", len(all_alarms))

    # Local imports keep the top of this file tidy and avoid name collisions
    # in the re-export block above.
    from report_analysis import (
        _gbc_alarms_in_window, _trips_from_gbc_alarms, _alarm_time,
        fetch_track, summarise_stops, summarise_yard,
        summarise_events, build_history,
    )
    from jimi import fetch_parking

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
            __import__("json").dumps(meta, indent=2), encoding="utf-8")
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