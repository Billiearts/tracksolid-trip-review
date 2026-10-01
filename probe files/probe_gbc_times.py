"""
probe_gbc_times.py — print the timestamps of every TRIP TIME GBC event.
"""
from datetime import datetime, timezone, timedelta
from jimi import api_call
from report import history_window, _is_gbc_alarm

IMEI = "865135060469358"                       # GN 2917 - 24
REF  = datetime(2026, 9, 21, 3, 24, 57, tzinfo=timezone.utc)

begin, end = history_window(REF)
print(f"window: {begin} -> {end}\n")

cur = begin
all_alarms = []
while cur < end:
    nxt = min(cur + timedelta(days=7), end)
    try:
        r = api_call("jimi.device.alarm.list", {
            "imeis": IMEI,
            "begin_time": cur.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":   nxt.strftime("%Y-%m-%d %H:%M:%S"),
        })
        all_alarms.extend(r.get("result") or [])
    except Exception:
        pass
    cur = nxt + timedelta(seconds=1)

gbc = [a for a in all_alarms if _is_gbc_alarm(a)]
gbc.sort(key=lambda a: a["alertTime"])

print(f"TOTAL GBC events: {len(gbc)}\n")
for a in gbc:
    print(f"  {a['alertTime']}  {a.get('alertTypeId')}  "
          f"geoid={a.get('geoid')}  name={a.get('alarmTypeName')}")