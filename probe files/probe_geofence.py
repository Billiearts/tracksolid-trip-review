"""
probe_geofence.py — one-off probe.

Calls a handful of plausible geofence-related API methods and prints the
raw responses, so we can see which ones exist and what they return.
Nothing is written to disk.
"""

import json
from jimi import api_call, USER_ID


# The first device IMEI we know about from the demo — change if you like.
TEST_IMEI = "865135061396881"


def try_call(label: str, method: str, params: dict) -> None:
    print(f"\n========== {label} ==========")
    print(f"method: {method}")
    print(f"params: {params}")
    try:
        response = api_call(method, params)
        print(json.dumps(response, indent=2, ensure_ascii=False)[:4000])
        # Show truncated so the terminal doesn't explode if a method
        # returns thousands of records.
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")


def main() -> None:
    # 1. Geofences defined on the account.
    try_call(
        "Account geofences",
        "jimi.user.geofence.list",
        {"target": USER_ID},
    )

    # 2. Geofences attached to a specific device.
    try_call(
        "Device geofences",
        "jimi.device.geofence.list",
        {"imei": TEST_IMEI},
    )

    # 3. Recent alarms — we don't know the alertTypeIds yet, so just
    #    pull the last 24h and look at what's in there.
    try_call(
        "Recent alarms (last 24h)",
        "jimi.device.alarm.list",
        {
            "imeis":      TEST_IMEI,
            "begin_time": "2026-09-22 00:00:00",
            "end_time":   "2026-09-23 23:59:59",
        },
    )


if __name__ == "__main__":
    main()