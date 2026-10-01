"""
probe_geofence2.py — second probe.

The first probe found that the obvious 'geofence.list' methods don't exist,
but that jimi.device.alarm.list does. This probe:

  1. Pulls a wider window of alarms for the specific test device and
     dumps the distinct alertTypeIds + names it sees. If geofence events
     are surfaced as alarms, we'll spot them.
  2. Tries a handful of plausible dedicated geofence-event method names.
"""

import json
from datetime import datetime, timedelta, timezone

from jimi import api_call, USER_ID


TEST_IMEI = "865135061396881"


def utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def try_call(label: str, method: str, params: dict) -> None:
    print(f"\n========== {label} ==========")
    print(f"method: {method}")
    print(f"params: {params}")
    try:
        response = api_call(method, params)
        # Keep output manageable for large payloads
        text = json.dumps(response, indent=2, ensure_ascii=False)
        print(text[:3000] + ("\n... [truncated]" if len(text) > 3000 else ""))
        return response
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return None


def main() -> None:
    # -- 1. Widened alarm window (7 days) to find geofence-related types ----
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=7)

    resp = try_call(
        "Alarms — 7 days",
        "jimi.device.alarm.list",
        {
            "imeis":      TEST_IMEI,
            "begin_time": utc(begin),
            "end_time":   utc(end),
        },
    )

    if resp and resp.get("result"):
        # Summarise distinct alert types so we don't have to eyeball 100s.
        seen = {}
        for a in resp["result"]:
            atid  = a.get("alertTypeId")
            atname = a.get("alarmTypeName")
            seen.setdefault((atid, atname), 0)
            seen[(atid, atname)] += 1
        print("\n--- Distinct alertTypeIds seen in window ---")
        for (atid, atname), count in sorted(seen.items(), key=lambda x: str(x[0])):
            print(f"  {atid!s:20}  {atname!s:40}  x{count}")

    # -- 2. Try plausible dedicated geofence-event method names --------------
    candidate_methods = [
        ("Geofence events (device)",  "jimi.device.geofence.event.list"),
        ("Geofence alerts (device)",  "jimi.device.geofence.alarm.list"),
        ("Geofence events (user)",    "jimi.user.geofence.event.list"),
        ("Geofence zones (device)",   "jimi.device.geofence.list"),
        ("Geofence zones (account)",  "jimi.user.geofence.list"),
        ("Geofence enter/exit",       "jimi.device.geofence.enter.exit.list"),
        ("Fence list",                "jimi.fence.list"),
        ("Fence events",              "jimi.fence.event.list"),
    ]

    for label, method in candidate_methods:
        try_call(
            label,
            method,
            {
                "target":     USER_ID,
                "imei":       TEST_IMEI,
                "imeis":      TEST_IMEI,
                "begin_time": utc(begin),
                "end_time":   utc(end),
            },
        )


if __name__ == "__main__":
    main()