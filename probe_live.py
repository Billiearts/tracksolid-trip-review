import logging
from datetime import datetime, timedelta, timezone
from jimi import api_call
from services.devices import get_devices, classify_device

logging.basicConfig(level=logging.WARNING)

def fence_name(alarm: dict) -> str:
    name = (alarm.get("alarmTypeName") or "").strip()
    import re
    m = re.search(r"\(([^()]*)\)\s*$", name)
    return m.group(1).strip() if m else ""

def main():
    print("Fetching device list...")
    devices = [d for d in get_devices() if classify_device(d.get("deviceName") or "") == "regular"]
    print(f"Found {len(devices)} regular trucks. Starting scan (this will take a minute)...")

    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=7)
    begin_str = begin.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end.strftime("%Y-%m-%d %H:%M:%S")

    out_lines = []
    out_lines.append(f"Scan window: {begin_str} to {end_str}\n")
    out_lines.append("=" * 80)

    for i, dev in enumerate(devices):
        imei = dev["imei"]
        name = dev.get("deviceName") or imei
        
        try:
            r = api_call("jimi.device.alarm.list", {
                "imeis": imei,
                "begin_time": begin_str,
                "end_time": end_str,
                "page": 1, "page_size": 500,
            })
            result = r.get("result") or r.get("data") or []
            if isinstance(result, dict):
                result = result.get("list") or []
        except Exception as e:
            out_lines.append(f"[{i+1}/{len(devices)}] {name} ({imei}) - API ERROR: {e}")
            continue

        if not result:
            out_lines.append(f"[{i+1}/{len(devices)}] {name} ({imei}) - NO ALARMS RETURNED")
            continue

        # Sort newest to oldest
        result.sort(key=lambda a: a.get("alertTime") or "", reverse=True)

        # Run logic
        derived_state = "unknown"
        decisive_event = None
        
        for a in result:
            fn = fence_name(a)
            event = (a.get("alertTypeId") or "").lower()
            
            if fn in ("TRIP TIME GBC", "GBC TRIP TIME"):
                if event == "out":
                    derived_state = "heading_to_takoradi"
                    decisive_event = a
                    break
                if event == "in":
                    derived_state = "at_awaso"
                    decisive_event = a
                    break
            elif fn == "TAKORADI ENTRY":
                if event == "out":
                    derived_state = "heading_to_awaso"
                    decisive_event = a
                    break
                if event == "in":
                    derived_state = "at_takoradi"
                    decisive_event = a
                    break

        # Compile sample of newest 5 alarms to see what's happening
        sample = []
        for a in result[:5]:
            sample.append(f"    {a.get('alertTime')} | {a.get('alertTypeId')} | {fence_name(a)}")

        out_lines.append(f"[{i+1}/{len(devices)}] {name} ({imei})")
        out_lines.append(f"  -> DERIVED STATE: {derived_state}")
        
        if decisive_event:
            out_lines.append(f"  -> MATCHED EVENT: {decisive_event.get('alertTime')} | {decisive_event.get('alertTypeId')} | {fence_name(decisive_event)}")
        else:
            out_lines.append(f"  -> MATCHED EVENT: None found")
            
        out_lines.append("  -> NEWEST 5 RAW ALARMS:")
        out_lines.extend(sample)
        out_lines.append("-" * 40)

    with open("live_probe_results.txt", "w") as f:
        f.write("\n".join(out_lines))
    
    print("\nDone! Results written to live_probe_results.txt")

if __name__ == "__main__":
    main()