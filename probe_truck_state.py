"""
probe_truck_state.py — one-shot diagnostic for a set of trucks.

For each IMEI, prints:

  * `fleet_state.current_state()` — exactly what the sweeper will see
  * The 5 most recent fence events, for context
  * Whether the current group is in `auto_release.RELEASE_GROUPS`
    (i.e. would the next sweep release an open assignment?)
  * The open assignment row (if any) and the driver's rest status

Usage:
  python probe_truck_state.py
  python probe_truck_state.py 865135061393573
  python probe_truck_state.py 865135061393573 865135061397954

With no arguments, probes GT 8061 - 25 (865135061397954) and
GT 8573 - 25 (865135061393573).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from services import fleet_state
from services import auto_release
from services import drivers as drivers_svc
from services.supabase_client import admin_client
from jimi import fetch_alarms_chunked


DEFAULT_IMEIS = [
    "865135061397954",   # GT 8061 - 25
    "865135061393573",   # GT 8573 - 25
]


def _most_recent_fence_events(imei: str, n: int = 5) -> list[dict]:
    """Return up to `n` most recent fence events for an IMEI."""
    end = datetime.now(timezone.utc)
    begin = end - timedelta(hours=48)
    try:
        alarms = fetch_alarms_chunked(imei, begin, end) or []
    except Exception as exc:
        return [{"_error": str(exc)}]

    rows = []
    for a in alarms:
        kind = fleet_state._event_kind(a)
        if not kind:
            continue
        name = fleet_state._fence_name(a)
        geoid = fleet_state._fence_geoid(a)
        if not name and not geoid:
            continue
        when = fleet_state._parse_ts(
            a.get("alertTime") or a.get("alarmTime")
            or a.get("alarm_time") or a.get("time") or a.get("gpsTime")
        )
        if when is None:
            continue
        rows.append({
            "at": when,
            "kind": kind,
            "name": name,
            "geoid": geoid,
        })

    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:n]


def _open_assignment_for_imei(imei: str) -> dict | None:
    try:
        resp = (admin_client().table("truck_assignments")
                .select("id,driver_id,truck_id,imei,assigned_at")
                .eq("imei", imei)
                .is_("released_at", "null")
                .limit(1)
                .execute())
        rows = resp.data or []
        return rows[0] if rows else None
    except Exception:
        return None


def _driver_summary(driver_id: str | None) -> dict:
    if not driver_id:
        return {"driver_id": None, "name": None, "rest_until": None}
    try:
        d = drivers_svc.get_driver(driver_id)
    except Exception:
        d = None
    try:
        ru = drivers_svc.rest_until(driver_id)
    except Exception:
        ru = None
    return {
        "driver_id": driver_id,
        "name": d.get("full_name") if d else None,
        "rest_until": ru.isoformat() if ru else None,
    }


def probe(imei: str) -> None:
    print("=" * 72)
    print(f"IEMI: {imei}")
    print("=" * 72)

    # 1. What fleet_state sees
    try:
        state = fleet_state.current_state(imei)
    except Exception as exc:
        print(f"  fleet_state.current_state raised: {exc}")
        state = None

    if state is None:
        print("  current_state: None  (fetch failed — sweep will leave "
              "any open assignment alone)")
        return

    print("  current_state:")
    print(json.dumps(
        {k: (v.isoformat() if isinstance(v, datetime) else v)
         for k, v in state.items() if k != "raw_event"},
        indent=4,
    ))

    # 2. Will the sweep release?
    group = state.get("group")
    will_release = group in auto_release.RELEASE_GROUPS
    print(f"\n  Would the next sweep release an open assignment for this "
          f"truck?  {'YES' if will_release else 'NO'}")
    print(f"  Reason: group={group!r} "
          f"{'∈' if will_release else '∉'} RELEASE_GROUPS={sorted(auto_release.RELEASE_GROUPS)}")

    # 3. Recent fence events (context)
    print("\n  Recent fence events (last 48 h, newest first):")
    events = _most_recent_fence_events(imei)
    if not events:
        print("    (none)")
    for e in events:
        if "_error" in e:
            print(f"    error: {e['_error']}")
            continue
        ts = e["at"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"    {ts}  {e['kind']:<5}  {e['name']:<40}  {e['geoid']}")

    # 4. Open assignment + driver rest state
    print("\n  Open assignment:")
    row = _open_assignment_for_imei(imei)
    if not row:
        print("    (no open assignment)")
    else:
        print(f"    id={row['id']}")
        print(f"    truck_id={row['truck_id']}")
        print(f"    assigned_at={row['assigned_at']}")
        drv = _driver_summary(row.get("driver_id"))
        print(f"    driver={drv['name']} ({drv['driver_id']})")
        print(f"    driver rest_until={drv['rest_until'] or '(not resting)'}")

    print()


def main(argv: list[str]) -> int:
    imeis = argv or DEFAULT_IMEIS
    for imei in imeis:
        probe(imei)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))