"""
probe_fleet.py — inspect the fleet and optionally assign drivers to
trucks for testing auto-release + rest period.

This probe does its own fence/state detection from jimi alarms so it
does NOT depend on services.devices.current_fence_state() (which may
not exist on this install).

Usage:
  python probe_fleet.py
  python probe_fleet.py --assign 3 --group yard
  python probe_fleet.py --assign 2 --group maintenance
  python probe_fleet.py --json > fleet.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

from jimi import fetch_alarms_chunked, RateLimitExceeded
from services import devices as devices_svc
from services import drivers as drivers_svc


# How far back to look for the latest fence event. 24 h is enough for
# trucks that entered the yard overnight or went on a trip today.
LOOKBACK = timedelta(hours=24)

# Fence names as they appear in Tracksolid alarms. Extend as needed.
FENCE_PATTERNS = {
    "AWASO":     "yard",
    "TRIP TIME": "on_road",
    "WORKSHOP":  "maintenance",
    "FUEL":      "fueling",
    "LOAD":      "loading",
    "WASH":      "loading",
    "WAITING":   "waiting",
    "DISPATCH":  "waiting",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def classify_fence_name(name: str) -> str | None:
    if not name:
        return None
    upper = name.upper()
    for needle, group in FENCE_PATTERNS.items():
        if needle in upper:
            return group
    return None


def latest_fence_event(imei: str) -> dict | None:
    """Return the most recent fence enter/exit alarm, or None."""
    end = _now()
    begin = end - LOOKBACK
    try:
        alarms = fetch_alarms_chunked(imei, begin, end)
    except RateLimitExceeded:
        raise
    except Exception as exc:
        logging.warning("alarms fetch failed for %s: %s", imei, exc)
        return None

    # Keep only fence events, sort by time desc.
    fence_events = []
    for a in alarms or []:
        fence_name = (
            a.get("fenceName") or a.get("fence_name")
            or a.get("geofenceName") or a.get("geofence_name")
            or ""
        )
        event_type = (
            a.get("eventType") or a.get("event_type")
            or a.get("type") or ""
        )
        # Tracksolid uses numeric codes: 24=enter fence, 25=exit fence.
        et_u = str(event_type).upper()
        is_enter = et_u in ("24", "ENTER", "IN")
        is_exit = et_u in ("25", "EXIT", "OUT")
        if not (is_enter or is_exit):
            continue
        if not fence_name:
            continue
        ts = a.get("alarmTime") or a.get("alarm_time") or a.get("time")
        try:
            when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        fence_events.append({
            "when": when,
            "fence": fence_name,
            "enter": is_enter,
        })

    if not fence_events:
        return None
    fence_events.sort(key=lambda x: x["when"], reverse=True)
    return fence_events[0]


def classify_truck(imei: str) -> dict:
    ev = latest_fence_event(imei)
    if ev is None:
        return {"group": "unknown", "fence": None, "at": None}
    group = classify_fence_name(ev["fence"])
    if group is None:
        return {"group": "unknown", "fence": ev["fence"],
                "at": ev["when"]}
    if ev["enter"] and group == "on_road":
        # Inside the TRIP TIME fence = on a trip.
        return {"group": "on_road", "fence": ev["fence"],
                "at": ev["when"]}
    if ev["enter"] and group != "on_road":
        # Inside a yard-side fence.
        return {"group": group, "fence": ev["fence"], "at": ev["when"]}
    # Latest event is an EXIT — outside the fence we left.
    if group == "yard":
        # Left AWASO — on road.
        return {"group": "on_road", "fence": ev["fence"], "at": ev["when"]}
    # Left any other fence — back in the general area. Bucket as
    # "unknown" so we don't misassign.
    return {"group": "unknown", "fence": ev["fence"], "at": ev["when"]}


def load_devices():
    try:
        devs = devices_svc.get_devices_cached_only()
    except AttributeError:
        devs = None
    except Exception:
        devs = None
    if devs is None:
        devs = devices_svc.get_devices()
    return devs or []


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--assign", type=int, default=0)
    ap.add_argument("--group", default="yard",
                    choices=("yard", "maintenance", "fueling", "loading",
                             "waiting", "on_road", "unknown"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only inspect the first N trucks (faster smoke test)")
    args = ap.parse_args(argv)

    devices = load_devices()
    if args.limit:
        devices = devices[: args.limit]
    print(f"Loaded {len(devices)} devices.\n")

    buckets = defaultdict(list)
    for i, d in enumerate(devices, 1):
        name = (d.get("deviceName") or "").strip()
        imei = (d.get("imei") or "").strip()
        if not name or not imei:
            continue
        try:
            cat = devices_svc.classify_device(name)
        except Exception:
            cat = "regular"
        if cat != "regular":
            continue
        info = classify_truck(imei)
        buckets[info["group"]].append({
            "truck_id": name,
            "imei":     imei,
            "fence":    info["fence"],
            "at":       info["at"].isoformat() if info["at"] else None,
        })
        if i % 20 == 0:
            print(f"  ...inspected {i}/{len(devices)}")

    if args.json:
        print(json.dumps(buckets, indent=2))
    else:
        for group in ("on_road", "yard", "waiting", "loading",
                      "fueling", "maintenance", "unknown"):
            items = buckets.get(group, [])
            print(f"=== {group.upper()} ({len(items)}) ===")
            for it in items:
                at = it["at"] or "-"
                print(f"  {it['truck_id']:<24} fence={it['fence']} at={at}")
            print()

    if args.assign <= 0:
        return 0

    group_items = buckets.get(args.group, [])
    if not group_items:
        print(f"No trucks in group '{args.group}'.")
        return 1

    res = drivers_svc.list_drivers(page=1, per_page=500,
                                   include_inactive=False,
                                   include_deleted=False)
    free_drivers = [
        d for d in res["drivers"]
        if drivers_svc.current_assignment_for_driver(d["id"]) is None
    ]
    free_drivers.sort(key=lambda x: x["full_name"].lower())

    n = min(args.assign, len(group_items), len(free_drivers))
    print(f"Free drivers: {len(free_drivers)}  |  "
          f"Candidates to assign: {n}\n")

    for i in range(n):
        truck = group_items[i]
        drv = free_drivers[i]
        try:
            a = drivers_svc.assign_driver(
                driver_id=drv["id"], truck_id=truck["truck_id"],
                notes=f"probe assign ({args.group})",
                created_by=None, override_rest=False,
            )
            print(f"  ✓ {drv['full_name']:<28} → {truck['truck_id']:<24} "
                  f"(assignment {a['id']})")
        except drivers_svc.DriverError as exc:
            print(f"  ✗ {drv['full_name']:<28} → {truck['truck_id']:<24}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))