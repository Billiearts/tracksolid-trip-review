import json
from datetime import datetime, timedelta, timezone
from jimi import api_call
from services.devices import get_devices

def main():
    query = input("Enter Truck Name or IMEI (e.g. GT 8573 - 25): ").strip()
    imei = None
    for d in get_devices():
        if query.lower() in (d.get("deviceName") or "").lower() or query == d.get("imei"):
            imei = d["imei"]
            print(f"Found: {d.get('deviceName')} ({imei})")
            break
            
    if not imei:
        print("Truck not found.")
        return

    print("\nFetching location.get...")
    loc_resp = api_call("jimi.device.location.get", {"imeis": imei})
    
    print("Fetching track.list (last 24h)...")
    end = datetime.now(timezone.utc)
    begin = end - timedelta(days=1)
    track_resp = api_call("jimi.device.track.list", {
        "imei": imei,
        "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
        "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
    })

    out = {
        "location_get": loc_resp,
        "track_list": track_resp
    }
    
    with open("raw_json_dump.txt", "w") as f:
        json.dump(out, f, indent=2)
        
    print("\nDone! Saved to raw_json_dump.txt")
    print("Please open that file and paste the contents here.")

if __name__ == "__main__":
    main()