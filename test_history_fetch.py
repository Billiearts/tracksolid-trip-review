"""
test_history_fetch.py — one-off diagnostic for the history-fetch optimisation.

Run 2 (active truck: GT 8009 - 25, IMEI 865135061397855).

Answers:
  1. How many trips / how many track points does an actively-running
     truck have in 3 months of history?
  2. What is the real max window for jimi.device.track.list? We probe
     24h / 7d / 14d / 30d in sequence and record which succeed.
  3. Does jimi.open.platform.report.parking accept the full 3-month
     window in one call?
  4. What is the CURRENT per-trip history cost?
  5. What would a SAMPLED history cost (first N trips + batched parking)?

Usage:
    python test_history_fetch.py

Reads .env via config.py, so TRACKSOLID_* creds must be set.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from config import TEMPLATE_PATH  # noqa: F401 — forces config.py to load .env
from jimi import fetch_alarms_chunked, fetch_parking
from report_analysis import (
    fetch_track,
    _gbc_alarms_in_window, _trips_from_gbc_alarms,
    history_window,
)


logging.basicConfig(
    level=logging.WARNING,           # quieter — we print our own progress
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Show httpx only on error, otherwise the log is unreadable.
logging.getLogger("httpx").setLevel(logging.ERROR)
logging.getLogger("httpcore").setLevel(logging.ERROR)


IMEI  = "865135061397855"
TRUCK = "GT 8009 - 25"


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _timed(label: str, fn, *args, **kwargs):
    t0 = time.monotonic()
    try:
        result = fn(*args, **kwargs)
        elapsed = time.monotonic() - t0
        print(f"  {label}: OK  ({elapsed:.2f}s)")
        return result, elapsed
    except Exception as exc:
        elapsed = time.monotonic() - t0
        print(f"  {label}: FAILED  ({elapsed:.2f}s) — "
              f"{type(exc).__name__}: {exc}")
        return None, elapsed


def main() -> None:
    print(f"=== History-fetch diagnostic for {TRUCK} ({IMEI}) ===\n")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    hist_begin, hist_end = history_window(now)
    print(f"History window: {_fmt(hist_begin)} → {_fmt(hist_end)}")
    print(f"Window length: {(hist_end - hist_begin).days} days\n")

    # ---- 1. Alarms / trips -----------------------------------------------
    print("[1] Alarm fetch over the full history window (chunked, 7-day)")
    alarms, t_alarms = _timed(
        "fetch_alarms_chunked",
        fetch_alarms_chunked, IMEI, hist_begin, hist_end,
    )
    if alarms is None:
        print("  -> cannot continue without alarms\n")
        return
    print(f"  -> {len(alarms)} alarms ({t_alarms:.1f}s)\n")

    print("[2] Trip extraction")
    gbc = _gbc_alarms_in_window(alarms, hist_begin, hist_end)
    trips = _trips_from_gbc_alarms(gbc)
    print(f"  GBC alarms: {len(gbc)}")
    print(f"  Trips:      {len(trips)}")

    durations_h = [t["duration_s"] / 3600 for t in trips]
    if durations_h:
        print(f"  Trip duration: min={min(durations_h):.2f}h  "
              f"median={sorted(durations_h)[len(durations_h)//2]:.2f}h  "
              f"max={max(durations_h):.2f}h  "
              f"total={sum(durations_h):.1f}h")

    # How many trips exceed the *suspected* 24h cap?
    over_24h = sum(1 for d in durations_h if d > 24)
    print(f"  Trips over 24h: {over_24h} / {len(trips)}")
    print()

    # ---- 3. Probe max window for track endpoint --------------------------
    print("[3] Probing jimi.device.track.list max window")
    # Use a mid-history anchor so we're not biased by "right now".
    probe_anchor = hist_begin + (hist_end - hist_begin) / 2

    for label, delta in (
        ("24h",  timedelta(hours=24)),
        ("7d",   timedelta(days=7)),
        ("14d",  timedelta(days=14)),
        ("30d",  timedelta(days=30)),
        ("60d",  timedelta(days=60)),
    ):
        b = probe_anchor
        e = probe_anchor + delta
        if e > now:
            e = now
        track, t = _timed(
            f"track span={label}",
            fetch_track, IMEI, b, e,
        )
        if track is not None:
            print(f"    -> {len(track)} points")
            # If the API silently caps, points would be suspiciously few
            # for the window length. Report points/hour so we can see it.
            hours = (e - b).total_seconds() / 3600
            if hours > 0:
                print(f"    -> {len(track)/hours:.1f} points/hour")
    print()

    # ---- 4. Parking full-window probe ------------------------------------
    print("[4] Parking over the full 3-month window (single call)")
    parking_full, t_parking = _timed(
        "fetch_parking(3mo)",
        fetch_parking, IMEI, hist_begin, hist_end,
    )
    if parking_full is not None:
        print(f"  -> {len(parking_full)} parking rows ({t_parking:.1f}s)")
    print()

    # ---- 5. CURRENT per-trip cost ----------------------------------------
    print(f"[5] CURRENT per-trip fetch (track + parking × {len(trips)} trips)")
    per_trip_times  = []
    per_trip_points = []
    per_trip_rows   = []
    for i, t in enumerate(trips):
        track, tt = _timed(
            f"  trip {i+1}/{len(trips)} track",
            fetch_track, IMEI, t["start"], t["end"],
        )
        parking, tp = _timed(
            f"  trip {i+1}/{len(trips)} parking",
            fetch_parking, IMEI, t["start"], t["end"],
        )
        if track is not None:
            per_trip_times.append(tt + tp)
            per_trip_points.append(len(track))
            per_trip_rows.append(len(parking or []))

    total_per_trip = sum(per_trip_times)
    print(f"  -> {len(per_trip_times)} successful trips, "
          f"{sum(per_trip_points)} track points, "
          f"{sum(per_trip_rows)} parking rows")
    print(f"  -> total {total_per_trip:.1f}s "
          f"(avg {total_per_trip/max(1, len(per_trip_times)):.2f}s/trip)\n")

    # ---- 6. SAMPLED history cost (what I propose) ------------------------
    print("[6] SAMPLED history cost (first N trips get track; "
          "parking batched once)")
    N = 10
    sample = trips[:N]
    sample_times  = []
    sample_points = 0
    for i, t in enumerate(sample):
        track, tt = _timed(
            f"  sample {i+1}/{len(sample)} track",
            fetch_track, IMEI, t["start"], t["end"],
        )
        sample_times.append(tt)
        if track is not None:
            sample_points += len(track)

    total_sampled = sum(sample_times) + (t_parking or 0.0)
    print(f"  -> sample size {len(sample)}/{len(trips)}")
    print(f"  -> track points: {sample_points}")
    print(f"  -> total fetch time: {total_sampled:.1f}s\n")

    # ---- 7. Summary ------------------------------------------------------
    print("=== SUMMARY ===")
    if per_trip_times:
        print(f"  Current per-trip: {len(per_trip_times)*2} calls, "
              f"{total_per_trip:.1f}s, {sum(per_trip_points)} points")
    print(f"  Sampled (N={N}):  {len(sample)} calls + 1 parking call, "
          f"{total_sampled:.1f}s, {sample_points} points")

    if per_trip_times and total_sampled > 0:
        speedup = total_per_trip / total_sampled
        print(f"  -> projected speedup: {speedup:.1f}×")

    print()
    print("API-cap conclusions (from [3]):")
    print("  - Any window that returned FAILED 'Time span over specified range'")
    print("    is above the real cap.")
    print("  - If 24h succeeds and 7d fails, cap = 24h.")
    print("  - If 7d succeeds and 14d fails, cap = 7d (the documented value).")


if __name__ == "__main__":
    main()