"""
probe_older_gbc.py — do GBC events exist before Sept 10?
"""
from datetime import datetime, timezone
from jimi import api_call

IMEI = "865135060469358"
GEOID = "791a3bb604ef41cfa302e5dc8f2c356c"

# Test a range we KNOW had driving (from the overspeed list).
WINDOWS = [
    ("2026-07-04", "2026-07-06"),   # overspeed on 07-05
    ("2026-08-06", "2026-08-08"),   # overspeed on 08-07
    ("2026-08-19", "2026-08-21"),   # overspeed on 08-20
    ("2026-09-04", "2026-09-06"),   # overspeed on 09-05
]

for start, end in WINDOWS:
    try:
        r = api_call("jimi.device.alarm.list", {
            "imeis":      IMEI,
            "begin_time": start + " 00:00:00",
            "end_time":   end   + " 23:59:59",
        })
        batch = r.get("result") or []
    except Exception as e:
        print(f"{start}..{end}  FAILED: {e}")
        continue

    gbc = [a for a in batch if (a.get("geoid") or "") == GEOID]
    overs = [a for a in batch
             if (a.get("alertTypeId") or "").lower() == "overspeed"]
    print(f"{start}..{end}:  total={len(batch)}  "
          f"GBC={len(gbc)}  overspeed={len(overs)}")
    if gbc:
        for a in gbc[:5]:
            print(f"      {a['alertTime']}  {a.get('alertTypeId')}")