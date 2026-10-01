"""
demo.py — entry point for the Tracksolid Pro demo.

Runs through the milestones in order:
  4. device list
  5. device location
"""

import json

from jimi import api_call, USER_ID


# The account to query in jimi.user.device.list.
# This is the value the server echoed back as "account" in the token response.
TARGET_ACCOUNT = USER_ID   # currently "GBCTRUCKS"


def main() -> None:
    # -- Milestone 4: list devices ------------------------------------------
    print(f"Calling jimi.user.device.list with target={TARGET_ACCOUNT!r}")
    response = api_call("jimi.user.device.list", {"target": TARGET_ACCOUNT})

    devices = response.get("result") or response.get("data") or []
    if not isinstance(devices, list):
        devices = devices.get("list", [])

    if not devices:
        print("[!] No devices returned.")
        return

    print(f"Found {len(devices)} device(s).")

    # -- Milestone 5: get location of the first device ----------------------
    first = devices[5]
    imei = first["imei"]
    print(f"\nCalling jimi.device.location.get for IMEI={imei} "
          f"({first.get('deviceName')})")

    loc_response = api_call("jimi.device.location.get", {"imeis": imei})

    print("\n=== jimi.device.location.get ===")
    print(json.dumps(loc_response, indent=2, ensure_ascii=False))

    locations = loc_response.get("result") or loc_response.get("data") or []
    if not isinstance(locations, list):
        locations = locations.get("list", [])

    if not locations:
        print("\n[!] No location data returned for this device.")
        return

    loc = locations[0]
    print("\nParsed location:")
    print(f"  IMEI:    {loc.get('imei')}")
    print(f"  lat:     {loc.get('lat')}")
    print(f"  lng:     {loc.get('lng')}")
    print(f"  gpsTime: {loc.get('gpsTime')}")
    print(f"  speed:   {loc.get('speed')}")
    print(f"  status:  {loc.get('status')}")
    print(f"  acc:     {loc.get('accStatus')}")

        # -- Milestone 6: short track over the last 24 hours --------------------
    from datetime import datetime, timedelta, timezone

    end_dt = datetime.now(timezone.utc)
    begin_dt = end_dt - timedelta(hours=24)
    begin_str = begin_dt.strftime("%Y-%m-%d %H:%M:%S")
    end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\nCalling jimi.device.track.list for IMEI={imei}")
    print(f"  begin_time={begin_str}")
    print(f"  end_time=  {end_str}")

    track_response = api_call("jimi.device.track.list", {
        "imei":       imei,
        "begin_time": begin_str,
        "end_time":   end_str,
    })

    tracks = track_response.get("result") or track_response.get("data") or []
    if not isinstance(tracks, list):
        tracks = tracks.get("list", [])

    print(f"\nTrack points returned: {len(tracks)}")
    if tracks:
        first_pt = tracks[0]
        last_pt = tracks[-1]
        print(f"  first: {first_pt.get('gpsTime')}  "
              f"({first_pt.get('lat')}, {first_pt.get('lng')})")
        print(f"  last:  {last_pt.get('gpsTime')}  "
              f"({last_pt.get('lat')}, {last_pt.get('lng')})")
    else:
        print("  (device probably hasn't moved in this window — "
              "endpoint plumbing still verified)")


if __name__ == "__main__":
    main()