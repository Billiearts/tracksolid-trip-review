"""
run_report.py — interactive driver for report.build_report().
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from config import TEMPLATE_PATH, OUTPUT_DIR
from report import build_report, utc_str, MIN_STOP_SECONDS
from services.devices import find_device


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def prompt_date(prompt: str, default: datetime) -> datetime:
    raw = input(f"{prompt} [default {utc_str(default)}]: ").strip()
    if not raw:
        return default
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        print("[!] Bad format. Expected 'YYYY-MM-DD HH:MM:SS'. Using default.")
        return default


def main() -> None:
    print("=== Tracksolid Pro — Trip Review report ===\n")

    query = input("Truck ID or IMEI: ").strip()
    if not query:
        print("No truck given, aborting.")
        return
    device = find_device(query)
    if not device:
        print(f"[!] No unique device matched '{query}'.")
        return

    imei        = device["imei"]
    device_name = device.get("deviceName") or imei
    driver_name = device.get("driverName") or ""

    now = datetime.now(timezone.utc).replace(microsecond=0)
    default_begin = now - timedelta(days=7)
    default_end   = now

    begin = prompt_date("Begin (UTC, YYYY-MM-DD HH:MM:SS)", default_begin)
    end   = prompt_date("End   (UTC, YYYY-MM-DD HH:MM:SS)", default_end)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(ch if ch.isalnum() or ch in " -" else "_"
                        for ch in device_name).strip()

    run_stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    out_path = OUTPUT_DIR / (
        f"Trip Review - {safe_name} - {begin.date()} - {run_stamp}.xlsx"
    )

    summary = build_report(
        imei=imei, truck_id=device_name, driver_name=driver_name,
        begin=begin, end=end,
        template_path=TEMPLATE_PATH, output_path=out_path,
    )

    min_mins = MIN_STOP_SECONDS // 60
    print("\n=== Summary ===")
    print(f"Truck:        {device_name} ({imei})")
    print(f"Driver:       {driver_name or '(none)'}")
    print(f"Trips found:  {len(summary['trips'])}")
    for i, t in enumerate(summary["trips"], start=1):
        print(f"  Trip {i}: {utc_str(t['start'])}  →  {utc_str(t['end'])}"
              f"   ({t['duration_s']/3600:.2f}h)")
    print(f"Alarms seen:  {summary['alarm_count']}")
    print(f"Track points: {summary['track_count']}")
    print(f"Stops >= {min_mins}m:  {summary['stops']['count']}  "
          f"(total {summary['stops']['total_seconds']/3600:.2f}h)")
    if summary["history"]:
        h = summary["history"]
        print(f"\nHistory ({h['trip_count']} trips, window "
              f"{utc_str(h['window_begin'])} → {utc_str(h['window_end'])}):")
        print(f"  Avg stops/trip:  {h['stops']['avg_count']:.1f}")
        print(f"  Avg stop hours:  {h['stops']['avg_seconds']/3600:.2f}h")
        print(f"  Longest stop:    {h['stops']['max_seconds']/3600:.2f}h")
    print(f"\nWrote: {out_path}")


if __name__ == "__main__":
    main()