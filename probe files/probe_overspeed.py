# probe_overspeed.py
import json
from datetime import datetime, timedelta, timezone
from jimi import api_call

imei = "865135061396881"
end = datetime.now(timezone.utc)
begin = end - timedelta(days=7)

r = api_call("jimi.device.alarm.list", {
    "imeis": imei,
    "begin_time": begin.strftime("%Y-%m-%d %H:%M:%S"),
    "end_time":   end.strftime("%Y-%m-%d %H:%M:%S"),
})
overs = [a for a in (r.get("result") or [])
         if (a.get("alertTypeId") or "").lower() == "overspeed"]
print(f"{len(overs)} overspeed alarms")
for a in overs[:3]:
    print(json.dumps({
        "time": a.get("alertTime"),
        "speed": a.get("speed"),
        "lat": a.get("lat"),
        "lng": a.get("lng"),
    }, indent=2))