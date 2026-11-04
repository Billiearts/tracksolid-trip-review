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

Prefetched alarms:
    `compute_report()` accepts an optional `alarms` list and optional
    `prefetch_begin` / `prefetch_end` bounds. The preview job worker
    fetches the union-of-search-and-history alarms once per IMEI and
    passes them in, so a 390-trip job with history on only fetches the
    3-month alarm window once per truck instead of once per trip.
"""

from __future__ import annotations

import logging
import time
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
                   include_history: bool = True,
                   alarms: list[dict] | None = None,
                   prefetch_begin: datetime | None = None,
                   prefetch_end: datetime | None = None) -> dict:
    """Compute one trip's report.

    `alarms` / `prefetch_begin` / `prefetch_end`:
        When the preview job worker calls this, it passes the union of
        search-and-history alarms it already fetched (once per IMEI) plus
        the [prefetch_begin, prefetch_end] window those alarms cover. If
        the prefetch covers the union this compute needs, we reuse it
        verbatim and skip the fetch. Otherwise we fall back to fetching
        our own — that's the CLI path and the 1-trip preview path.
    """
    t_start_wall = time.monotonic()
    timings: dict[str, float] = {}

    if include_history:
        hist_begin, hist_end = history_window(begin)
        union_begin = min(begin, hist_begin)
        union_end   = max(end,   hist_end)
    else:
        hist_begin = hist_end = None
        union_begin = begin - YARD_LOOKBACK
        union_end   = end

    # ---- stage 1: alarms (reuse if the prefetch covers the union) ----
    if alarms is not None and prefetch_begin is not None and prefetch_end is not None:
        if prefetch_begin <= union_begin and prefetch_end >= union_end:
            all_alarms = alarms
            timings["alarms"] = 0.0
            log.info("[1/6] Reusing prefetched alarms (%d, window %s → %s)",
                     len(all_alarms),
                     utc_str(prefetch_begin), utc_str(prefetch_end))
        else:
            log.info("[1/6] Prefetch window %s → %s does not cover %s → %s; "
                     "refetching",
                     utc_str(prefetch_begin), utc_str(prefetch_end),
                     utc_str(union_begin), utc_str(union_end))
            t0 = time.monotonic()
            from jimi import fetch_alarms_chunked
            all_alarms = fetch_alarms_chunked(imei, union_begin, union_end)
            timings["alarms"] = time.monotonic() - t0
            log.info("      %d alarms fetched (%.1fs)",
                     len(all_alarms), timings["alarms"])
    elif alarms is not None:
        all_alarms = alarms
        timings["alarms"] = 0.0
        log.info("[1/6] Reusing prefetched alarms (%d)", len(all_alarms))
    else:
        t0 = time.monotonic()
        log.info("[1/6] Fetching alarms for %s → %s ...",
                 utc_str(union_begin), utc_str(union_end))
        try:
            from jimi import fetch_alarms_chunked
            all_alarms = fetch_alarms_chunked(imei, union_begin, union_end)
        except RateLimitExceeded:
            raise
        timings["alarms"] = time.monotonic() - t0
        log.info("      %d alarms fetched (%.1fs)",
                 len(all_alarms), timings["alarms"])

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

    # ---- stage 2: track ----
    t0 = time.monotonic()
    log.info("[2/6] Fetching track ...")
    try:
        track = fetch_track(imei, trip_start or begin,
                            trip_end or end) if trip_start else []
    except RateLimitExceeded:
        track = []
    timings["track"] = time.monotonic() - t0
    log.info("      %d track points (%.1fs)",
             len(track), timings["track"])

    # ---- stage 3: parking ----
    t0 = time.monotonic()
    log.info("[3/6] Fetching parking events ...")
    try:
        parking = fetch_parking(imei, trip_start or begin,
                                trip_end or end) if trip_start else []
    except RateLimitExceeded:
        parking = []
    timings["parking"] = time.monotonic() - t0
    log.info("      %d parking events (%.1fs)",
             len(parking), timings["parking"])

    # ---- stage 4: filter alarms ----
    log.info("[4/6] Filtering alarms for trip window ...")
    if trip_start and trip_end:
        alarms_in_trip = [
            a for a in all_alarms
            if (tt := _alarm_time(a)) is not None
            and trip_start <= tt <= trip_end
        ]
    else:
        alarms_in_trip = []
    log.info("      %d alarms in trip", len(alarms_in_trip))

    # ---- stage 5: summarise ----
    t0 = time.monotonic()
    log.info("[5/6] Summarising ...")
    stops  = summarise_stops(track, parking, trip_start, trip_end, debug=False)
    events = summarise_events(alarms_in_trip)
    yard   = summarise_yard(all_alarms, trip_start)
    timings["summarise"] = time.monotonic() - t0
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

    # ---- stage 6: history ----
    history = None
    if include_history and trip_start:
        t0 = time.monotonic()
        log.info("[6/6] Building 3-month history ...")
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
            history = None
        timings["history"] = time.monotonic() - t0
        log.info("      history stage took %.2fs", timings["history"])

    timings["total"] = time.monotonic() - t_start_wall
    log.info(
        "compute_report(%s) timings: alarms=%.1fs track=%.1fs parking=%.1fs "
        "summarise=%.1fs history=%.1fs total=%.1fs",
        truck_id,
        timings.get("alarms", 0),
        timings.get("track", 0),
        timings.get("parking", 0),
        timings.get("summarise", 0),
        timings.get("history", 0),
        timings["total"],
    )

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
        "alarm_count":   len(alarms_in_trip),
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