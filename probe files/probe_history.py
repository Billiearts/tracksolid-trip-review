"""
probe_history.py — compare our trip detection against the raw alarm stream.
"""
from datetime import datetime, timezone
from jimi import api_call
from report import history_window, parse_utc, _is_gbc_alarm

IMEI = "865135060469358"           # GN 2917 - 24
REF  = datetime(2026, 9, 21, 3, 24, 57, tzinfo=timezone.utc)

begin, end = history_window(REF)
print(f"window: {begin} -> {end}\n")

# Chunk the alarm query.
from datetime import timedelta
cur = begin
all_alarms = []
while cur < end:
    nxt = min(cur + timedelta(days=7), end)
    try:
        r = api_call("jimi.device.alarm.list", {
            "imeis":      IMEI,
            "begin_time": cur.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":   nxt.strftime("%Y-%m-%d %H:%M:%S"),
        })
        batch = r.get("result") or []
        all_alarms.extend(batch)
        print(f"  {cur:%Y-%m-%d} -> {nxt:%Y-%m-%d}  {len(batch)} alarms")
    except Exception as e:
        print(f"  {cur:%Y-%m-%d} -> {nxt:%Y-%m-%d}  FAILED: {e}")
    cur = nxt + timedelta(seconds=1)

print(f"\nTotal alarms: {len(all_alarms)}")

# Overspeed breakdown
overs = [a for a in all_alarms
         if (a.get("alertTypeId") or "").lower() == "overspeed"]
print(f"\nOverspeed alarms total: {len(overs)}")
by_bucket = {"speeding_10": [], "speeding_20": [], "speeding_over_20": []}
for o in overs:
    try:
        s = float(o.get("speed") or 0)
    except (TypeError, ValueError):
        continue
    over = s - 75
    if over <= 0:            continue
    elif over < 10:          by_bucket["speeding_10"].append((o["alertTime"], s))
    elif over < 20:          by_bucket["speeding_20"].append((o["alertTime"], s))
    else:                    by_bucket["speeding_over_20"].append((o["alertTime"], s))

for k, v in by_bucket.items():
    print(f"\n{k}: {len(v)} events")
    for t, s in sorted(v):
        print(f"    {t}  {s} kph")

# GBC event count
gbc = [a for a in all_alarms if _is_gbc_alarm(a)]
gbc.sort(key=lambda a: a["alertTime"])
ins = sum(1 for a in gbc if (a.get("alertTypeId") or "").lower() == "in")
outs = sum(1 for a in gbc if (a.get("alertTypeId") or "").lower() == "out")
print(f"\nGBC events: {ins} 'in', {outs} 'out'")