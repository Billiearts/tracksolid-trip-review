"""
probe_all.py — discover every geofence firing on the account.

Works from any directory: adds the project root to sys.path so `jimi`,
`services`, and `config` are importable.

Run from project root:  python "probe files\probe_all.py"
Run from probe files:   python probe_all.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# --- project-root shim ----------------------------------------------------
_HERE = Path(__file__).resolve().parent
if (_HERE / "jimi.py").exists():
    _ROOT = _HERE
elif (_HERE.parent / "jimi.py").exists():
    _ROOT = _HERE.parent
else:
    raise SystemExit(
        f"Could not find jimi.py near {_HERE}. "
        "Run this from the project root or fix the path."
    )
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# --- normal imports (now resolvable) --------------------------------------
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from jimi import api_call, USER_ID


# --- tunables -------------------------------------------------------------
DAYS = 60
MAX_DEVICES = 10
USE_ALL_DEVICES = False
CHUNK_DAYS = 7


def _utc(dt): return dt.strftime("%Y-%m-%d %H:%M:%S")


def _get_devices() -> list[dict]:
    """Prefer services.devices (cached, paginated). Fall back to a direct
    call if that module can't be imported for any reason."""
    try:
        from services.devices import get_devices
        return get_devices() or []
    except Exception as exc:
        print(f"[probe] services.devices failed ({exc}); "
              f"falling back to direct device list")
        try:
            r = api_call("jimi.user.device.list", {"target": USER_ID})
        except Exception as exc2:
            print(f"[probe] direct device list also failed: {exc2}")
            return []
        result = r.get("result")
        if isinstance(result, dict):
            result = result.get("list") or result.get("items") or []
        return result or []


def _strip_in_out_suffix(name: str) -> str:
    n = (name or "").strip()
    for suffix in (" in", " out", " enter", " exit", " -in", " -out",
                   " (in)", " (out)", " [in]", " [out]"):
        if n.lower().endswith(suffix):
            return n[: -len(suffix)].strip()
    return n


# --- pass 1: direct listing ------------------------------------------------

def pass_direct_listing():
    print("=" * 66)
    print("PASS 1 — direct geofence-listing endpoints")
    print("=" * 66)

    candidates = [
        ("Account geofences",  "jimi.user.geofence.list", {"target": USER_ID}),
        ("Account fences",     "jimi.user.fence.list",    {"target": USER_ID}),
        ("Zone list",          "jimi.user.zone.list",     {"target": USER_ID}),
    ]
    for label, method, params in candidates:
        print(f"\n--- {label}  ({method}) ---")
        try:
            r = api_call(method, params)
            print(json.dumps(r, indent=2, ensure_ascii=False)[:1500])
        except Exception as e:
            print(f"[ERR] {type(e).__name__}: {e}")


# --- pass 2: stay / parking / zone events ---------------------------------

def pass_stay_like():
    print("\n" + "=" * 66)
    print("PASS 2 — stay / parking / zone event endpoints")
    print("=" * 66)

    devices = _get_devices()
    if not devices:
        print("  no devices")
        return
    imei = devices[0]["imei"]
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=7)

    candidates = [
        ("Parking report",  "jimi.open.platform.report.parking"),
        ("Stay report",     "jimi.open.platform.report.stay"),
        ("Geofence report", "jimi.open.platform.report.geofence"),
        ("Zone report",     "jimi.open.platform.report.zone"),
    ]
    for label, method in candidates:
        print(f"\n--- {label}  ({method}) ---")
        try:
            r = api_call(method, {
                "account":    USER_ID,
                "imeis":      imei,
                "imei":       imei,
                "start_time": _utc(begin),
                "end_time":   _utc(end),
                "start_row":  "1",
                "page_size":  "10",
            })
            print(json.dumps(r, indent=2, ensure_ascii=False)[:1500])
        except Exception as e:
            print(f"[ERR] {type(e).__name__}: {e}")


# --- pass 3: alarm scan ----------------------------------------------------

def pass_alarm_scan():
    print("\n" + "=" * 66)
    print(f"PASS 3 — alarm scan (last {DAYS} days, "
          f"{'all' if USE_ALL_DEVICES else MAX_DEVICES} device(s))")
    print("=" * 66)

    devices = _get_devices()
    if not devices:
        print("  no devices — cannot scan alarms")
        return
    targets = devices if USE_ALL_DEVICES else devices[:MAX_DEVICES]

    end   = datetime.now(timezone.utc)
    begin = end - timedelta(days=DAYS)

    fences: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "devices": set(), "alert_ids": defaultdict(int),
                 "example": None}
    )

    for i, d in enumerate(targets, start=1):
        imei = d.get("imei")
        name = d.get("deviceName") or imei
        if not imei:
            continue
        print(f"\n[{i}/{len(targets)}] {name}  ({imei})")
        cur = begin
        total_this_device = 0
        while cur < end:
            nxt = min(cur + timedelta(days=CHUNK_DAYS), end)
            try:
                r = api_call("jimi.device.alarm.list", {
                    "imeis":      imei,
                    "begin_time": _utc(cur),
                    "end_time":   _utc(nxt),
                })
                alarms = r.get("result") or []
                total_this_device += len(alarms)
                for a in alarms:
                    geoid = (a.get("geoid") or "").strip()
                    if not geoid:
                        continue
                    base = _strip_in_out_suffix(a.get("alarmTypeName") or "")
                    key = (geoid, base)
                    fences[key]["count"] += 1
                    fences[key]["devices"].add(imei)
                    fences[key]["alert_ids"][
                        (a.get("alertTypeId") or "").strip()
                    ] += 1
                    if fences[key]["example"] is None:
                        fences[key]["example"] = a
            except Exception as e:
                print(f"    [ERR] {type(e).__name__}: {e}")
            cur = nxt + timedelta(seconds=1)
        print(f"    {total_this_device} alarm(s)")

    print("\n" + "=" * 66)
    print(f"Discovered {len(fences)} distinct geofence(s)")
    print("=" * 66)

    ranked = sorted(fences.items(), key=lambda kv: -kv[1]["count"])
    for (geoid, base), info in ranked:
        print(f"\n  name:     {base!r}")
        print(f"  geoid:    {geoid}")
        print(f"  events:   {info['count']}  on {len(info['devices'])} device(s)")
        print(f"  alertIds: {dict(info['alert_ids'])}")
        ex = info["example"] or {}
        for k in ("alertTime", "address", "lat", "lng", "deviceName"):
            if k in ex:
                print(f"  sample {k}: {ex[k]!r}")

    print("\n\nNext step: match each name above to "
          "maintenance / weighing / fueling / waybill.")


if __name__ == "__main__":
    pass_direct_listing()
    pass_stay_like()
    pass_alarm_scan()