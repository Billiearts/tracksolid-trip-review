"""
Probe: dump the last 72h of fence events for one IMEI, and show what
current_fence_state() derived from them. Use this to diagnose "not at
the yard" when the truck is visibly in AWASO.
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta, timezone

from jimi import fetch_alarms
from report_analysis import (
    _alarm_time, _alarm_boundary_event, _alarm_fence_name,
)
from services import drivers as svc


def main(imei: str):
    now = datetime.now(timezone.utc)
    begin = now - timedelta(hours=72)

    alarms = fetch_alarms(
        imei,
        begin.strftime("%Y-%m-%d %H:%M:%S"),
        now.strftime("%Y-%m-%d %H:%M:%S"),
    ) or []

    print(f"IMEI {imei}: {len(alarms)} alarms between {begin} and {now}\n")

    print("Fence events (in/out only, newest last):")
    fence_rows = []
    for a in alarms:
        ev = _alarm_boundary_event(a)
        if ev is None:
            continue
        t = _alarm_time(a)
        if t is None:
            continue
        fence = _alarm_fence_name(a)
        geoid = (a.get("geoid") or "").strip()
        fence_rows.append((t, ev, fence, geoid))
    fence_rows.sort(key=lambda r: r[0])

    if not fence_rows:
        print("  (none)")
    for t, ev, fence, geoid in fence_rows:
        print(f"  {t.strftime('%Y-%m-%d %H:%M:%S')}  {ev:3s}  {fence!r}  geoid={geoid}")

    print("\nDerived state from current_fence_state():")
    state = svc.current_fence_state(imei, now=now)
    for k, v in state.items():
        print(f"  {k}: {v}")

    print("\nTruck-state block (_truck_has_open_trip via truck name):")
    from services.devices import find_device
    # Try to find a truck name for this IMEI so we can use the public path
    dev = None
    try:
        from services.devices import get_devices
        for d in get_devices():
            if d.get("imei") == imei:
                dev = d
                break
    except Exception:
        pass
    name = dev.get("deviceName") if dev else None
    if name:
        block = svc._truck_has_open_trip(name)
        print(f"  truck_id={name!r} -> {block}")
    else:
        print("  (could not resolve a truck name)")


if __name__ == "__main__":
    imei = sys.argv[1] if len(sys.argv) > 1 else "865135061392260"
    main(imei)