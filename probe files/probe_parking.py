# probe_parking.py
from jimi import api_call

CANDIDATES = [
    ("Parking report (device)",  "jimi.device.parking.list"),
    ("Parking report (user)",    "jimi.user.parking.list"),
    ("Stop list (device)",       "jimi.device.stop.list"),
    ("Stop report",              "jimi.device.stay.list"),
    ("Parking events",           "jimi.device.parking.event.list"),
]

for label, method in CANDIDATES:
    print(f"\n=== {label}: {method} ===")
    try:
        r = api_call(method, {
            "imei":       "865135060480249",
            "imeis":      "865135060480249",
            "begin_time": "2026-09-19 09:27:01",
            "end_time":   "2026-09-20 03:47:17",
        })
        print(f"code={r.get('code')} message={r.get('message')!r}")
        print(f"result count={len(r.get('result') or [])}")
    except Exception as e:
        print(f"[ERR] {e}")