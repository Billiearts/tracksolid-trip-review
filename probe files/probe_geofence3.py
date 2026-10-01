"""
probe_geofence3.py — dump one TRIP TIME GBC alarm in full.

The alarm list already tells us geofence events are surfaced there. This
dumps the raw JSON for one such alarm so we can see exactly which fields
carry the geofence identity (geoid, etc.) and whether we can filter on
something more stable than the alarmTypeName string.
"""

import json
from datetime import datetime, timedelta, timezone

from jimi import api_call


TEST_IMEI = "865135061396881"


def main() -> None:
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=7)

    response = api_call("jimi.device.alarm.list", {
        "imeis":      TEST_IMEI,
        "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
    })

    alarms = response.get("result") or []

    gbc_alarms = [
        a for a in alarms
        if "TRIP TIME GBC" in (a.get("alarmTypeName") or "")
    ]

    print(f"Total alarms:     {len(alarms)}")
    print(f"TRIP TIME GBC:    {len(gbc_alarms)}")
    print()

    if not gbc_alarms:
        print("[!] No TRIP TIME GBC alarms in this window.")
        return

    # Dump the first three so we see both "in" and "out" shapes.
    for i, a in enumerate(gbc_alarms[:3], start=1):
        print(f"--- GBC alarm #{i} ---")
        print(json.dumps(a, indent=2, ensure_ascii=False))
        print()

    # And a summary of every distinct field-value for GBC alarms.
    print("=== Distinct field values across all GBC alarms ===")
    keys = set()
    for a in gbc_alarms:
        keys.update(a.keys())
    for k in sorted(keys):
        values = {str(a.get(k)) for a in gbc_alarms}
        if len(values) <= 5:
            print(f"  {k:20}  {sorted(values)}")
        else:
            print(f"  {k:20}  ({len(values)} distinct values)")


if __name__ == "__main__":
    main()