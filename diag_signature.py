"""
probe_direction.py — compare Tracksolid's direction field to the live map's
corridor classification.

Pick a truck that has recently completed an Awaso→Takoradi or Takoradi→Awaso
run. The script fetches 6 hours of track, prints the bearing at each point,
and buckets it with the same ±45° rule the live map should adopt.
"""
import logging
from datetime import datetime, timedelta, timezone
from config import TEMPLATE_PATH  # noqa: F401 — loads .env
from report_analysis import fetch_track

logging.basicConfig(level=logging.WARNING)
logging.getLogger("httpx").setLevel(logging.ERROR)

IMEI = "865135061397855"      # GT 8009 - 25 — change if you prefer
HOURS = 6

now = datetime.now(timezone.utc)
begin = now - timedelta(hours=HOURS)

track = fetch_track(IMEI, begin, now)
print(f"{len(track)} points in last {HOURS}h\n")

# Corridor bearings, from the two facilities in the live map.
# Awaso is roughly NE of Takoradi on the map.
# Compute the bearing of the corridor once, then classify.
import math

def bearing(lat1, lng1, lat2, lng2):
    """Bearing from (lat1,lng1) to (lat2,lng2), 0=N, clockwise."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lng2 - lng1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1)*math.sin(p2) - math.sin(p1)*math.cos(p2)*math.cos(dl)
    b = math.degrees(math.atan2(x, y))
    return (b + 360) % 360

# Rough coordinates for the two ends. Replace with your fence centroids.
AWASO   = (6.2298,  -2.2847)   # NE end
TAKORADI = (4.8939, -1.7482)   # SW end

to_takoradi_bearing = bearing(*AWASO, *TAKORADI)      # ~SW, near 220°
to_awaso_bearing    = (to_takoradi_bearing + 180) % 360

print(f"Corridor bearing Awaso→Takoradi: {to_takoradi_bearing:.1f}°")
print(f"Corridor bearing Takoradi→Awaso: {to_awaso_bearing:.1f}°\n")

def classify(direction_deg, speed):
    if speed < 5:
        return "parked"
    d = float(direction_deg) % 360
    # angular distance to each corridor axis
    def ang_dist(a, b):
        x = abs(a - b) % 360
        return min(x, 360 - x)
    if ang_dist(d, to_takoradi_bearing) <= 45:
        return "heading_to_takoradi"
    if ang_dist(d, to_awaso_bearing) <= 45:
        return "heading_to_awaso"
    return "off_corridor"

from collections import Counter
counts = Counter()
for p in track:
    d = p.get("direction")
    s = p.get("gpsSpeed") or 0
    if d is None:
        continue
    counts[classify(d, float(s))] += 1

print("Buckets over the window:")
for k, v in counts.most_common():
    print(f"  {k:20s} {v:4d}")