"""
probe_stops.py — dump raw gpsSpeed / ignition / accStatus during the
stop windows the parking report identified.
"""

from jimi import api_call


IMEI     = "865135060480249"
BEGIN    = "2026-09-19 09:27:01"
END      = "2026-09-20 03:47:17"

# Known stops from the client's parking report.
STOPS = [
    ("2026-09-19 09:30:00", "2026-09-19 09:30:10", "start-up blips (5 rows)"),
    ("2026-09-19 15:32:25", "2026-09-19 15:45:43", "stop A (13:18)"),
    ("2026-09-19 17:28:24", "2026-09-19 17:34:53", "stop C+D (4:59 + 0:55)"),
    ("2026-09-19 18:33:04", "2026-09-19 18:56:34", "stop B (23:29)"),
    ("2026-09-19 20:27:56", "2026-09-19 20:32:44", "stop (4:47)"),
    ("2026-09-19 22:06:28", "2026-09-19 22:11:07", "stop (4:38)"),
    ("2026-09-19 22:11:07", "2026-09-20 03:47:17", "overnight park"),
]


def main() -> None:
    r = api_call("jimi.device.track.list", {
        "imei":       IMEI,
        "begin_time": BEGIN,
        "end_time":   END,
    })
    pts = r.get("result") or r.get("data") or []
    if isinstance(pts, dict):
        pts = pts.get("list", [])
    print(f"total points: {len(pts)}\n")

    for start, end, label in STOPS:
        in_window = [p for p in pts
                     if start <= (p.get("gpsTime") or "") <= end]
        print(f"=== {label}  ({start} → {end})  {len(in_window)} samples ===")
        if not in_window:
            print("  (no samples in this window!)")
            print()
            continue

        speeds = []
        for p in in_window:
            try:
                speeds.append(float(p.get("gpsSpeed") or 0))
            except (TypeError, ValueError):
                pass

        # Show first 5 raw samples with all fields
        for p in in_window[:5]:
            print(f"  {p.get('gpsTime')}  "
                  f"gpsSpeed={p.get('gpsSpeed')!r}  "
                  f"ignition={p.get('ignition')!r}  "
                  f"accStatus={p.get('accStatus')!r}")

        if speeds:
            print(f"  ---")
            print(f"  speed min={min(speeds):.1f}  "
                  f"median={sorted(speeds)[len(speeds)//2]:.1f}  "
                  f"max={max(speeds):.1f}  "
                  f"avg={sum(speeds)/len(speeds):.2f}")
            # how many are below various thresholds
            for thr in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0):
                n = sum(1 for s in speeds if s < thr)
                print(f"  < {thr:.1f} kph: {n} / {len(speeds)}")
        print()


if __name__ == "__main__":
    main()