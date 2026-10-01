"""
probe_events.py — find the alertTypeIds for driver-behaviour events.

Pulls alarms for several IMEIs over 7 days and prints any alertTypeId /
alarmTypeName pairs that look like driver-behaviour events (acceleration,
deceleration, braking, cornering, turn).
"""

import json
from datetime import datetime, timedelta, timezone

from jimi import api_call


IMEIS = [
    "865135061396881",   # GT 8228 - 25  (tested already)
    "865135061555239",   # Water Bowser GT 9120 - 26
    "865135061557508",   # GT 6990 - 25 CANTEEN VAN
    "865135061393573",   # GT 8573 - 25
    "351510093053547",   # GT 8626-25
]

INTERESTING = ("accel", "decel", "brake", "braking", "corner", "turn", "sharp")


def main() -> None:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=7)
    b = begin.strftime("%Y-%m-%d %H:%M:%S")
    e = end.strftime("%Y-%m-%d %H:%M:%S")

    seen: dict[tuple, int] = {}

    for imei in IMEIS:
        print(f"\n=== {imei} ===")
        try:
            resp = api_call("jimi.device.alarm.list", {
                "imeis": imei, "begin_time": b, "end_time": e,
            })
        except Exception as exc:
            print(f"  [error] {exc}")
            continue
        alarms = resp.get("result") or []
        print(f"  {len(alarms)} alarms")
        for a in alarms:
            key = (a.get("alertTypeId"), a.get("alarmTypeName"))
            seen[key] = seen.get(key, 0) + 1

    print("\n\n=== All distinct alert types across all trucks ===")
    for (atid, name), count in sorted(seen.items(), key=lambda x: str(x[0][0])):
        marker = "  <-- DRIVER-BEHAVIOUR?" if any(
            w in (name or "").lower() for w in INTERESTING
        ) else ""
        print(f"  {atid!s:15}  {name!s:45}  x{count}{marker}")


if __name__ == "__main__":
    main()