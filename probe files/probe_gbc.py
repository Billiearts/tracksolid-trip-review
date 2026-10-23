"""
probe_gbc_only.py — find every fence with 'GBC' or 'EXIT' or 'ENTER' in its name.
Scans all devices, past 90 days. Prints one line per distinct fence.
"""
from __future__ import annotations
import sys
from pathlib import Path
_HERE = Path(__file__).resolve().parent
for candidate in (_HERE, _HERE.parent):
    if (candidate / "jimi.py").exists():
        sys.path.insert(0, str(candidate)); break

from datetime import datetime, timedelta, timezone
from collections import defaultdict
from jimi import api_call
from services.devices import get_devices


DAYS = 90
CHUNK_DAYS = 7
NAME_HINTS = ("GBC", "EXIT", "ENTER", "YARD")


def _utc(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")


def main():
    devices = get_devices() or []
    print(f"Scanning {len(devices)} device(s), last {DAYS} days\n")

    end   = datetime.now(timezone.utc)
    begin = end - timedelta(days=DAYS)

    fences: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "devices": set(), "alert_ids": defaultdict(int)}
    )

    for i, d in enumerate(devices, start=1):
        imei = d.get("imei")
        if not imei:
            continue
        cur = begin
        while cur < end:
            nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
            try:
                r = api_call("jimi.device.alarm.list", {
                    "imeis": imei,
                    "begin_time": _utc(cur),
                    "end_time":   _utc(nxt),
                })
                for a in (r.get("result") or []):
                    geoid = (a.get("geoid") or "").strip()
                    name  = a.get("alarmTypeName") or ""
                    if not geoid: continue
                    if not any(h in name.upper() for h in NAME_HINTS):
                        continue
                    key = (geoid, name)
                    fences[key]["count"] += 1
                    fences[key]["devices"].add(imei)
                    fences[key]["alert_ids"][
                        (a.get("alertTypeId") or "").strip()
                    ] += 1
            except Exception as e:
                print(f"  [ERR] {d.get('deviceName')} {cur:%Y-%m-%d}: {e}")
            cur = nxt + timedelta(seconds=1)
        print(f"[{i}/{len(devices)}] {d.get('deviceName') or imei} done")

    print(f"\n{'='*70}\nDistinct GBC/EXIT/ENTER/YARD fences:\n{'='*70}")
    for (geoid, name), info in sorted(fences.items()):
        print(f"\n  name:     {name!r}")
        print(f"  geoid:    {geoid}")
        print(f"  events:   {info['count']}  on {len(info['devices'])} device(s)")
        print(f"  alertIds: {dict(info['alert_ids'])}")


if __name__ == "__main__":
    main()