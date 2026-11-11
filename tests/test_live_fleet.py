import pytest
from datetime import datetime, timezone, timedelta
from services.live_fleet import (
    _point_in_polygon, _classify_direction, _motion_state,
    _direction_hold_set, _direction_hold_get, _direction_hold_clear,
    _resolve_yard_membership, _haversine_km, _distance_to_facility_km
)

# A simple square polygon for testing (roughly around Awaso)
POLY = [
    [6.0, -2.3],
    [6.4, -2.3],
    [6.4, -2.1],
    [6.0, -2.1],
]
FACILITY = {"kind": "awaso", "shape": "polygon", "polygon": POLY}
AWASO = {"kind": "awaso", "lat": 6.2, "lng": -2.2}
TAKORADI = {"kind": "takoradi", "lat": 4.9, "lng": -1.7}
AXIS = {"to_takoradi_bearing": 225.0, "to_awaso_bearing": 45.0} # SW and NE

def test_point_in_polygon():
    assert _point_in_polygon(6.2, -2.2, POLY) is True
    assert _point_in_polygon(5.9, -2.2, POLY) is False
    assert _point_in_polygon(6.5, -2.2, POLY) is False

def test_motion_state():
    now = datetime.now(timezone.utc)
    fresh = now - timedelta(minutes=5)
    old = now - timedelta(hours=2)
    
    assert _motion_state(0, fresh, now, "0") == "parked"
    assert _motion_state(0, fresh, now, "1") == "idle"
    assert _motion_state(10, fresh, now, "1") == "moving"
    assert _motion_state(10, old, now, "1") == "offline"

def test_direction_hold():
    imei = "TEST_HOLD"
    _direction_hold_clear(imei)
    
    # 1. Set a hold
    _direction_hold_set(imei, "heading_to_takoradi", 6.2, -2.2)
    h = _direction_hold_get(imei)
    assert h["direction"] == "heading_to_takoradi"
    
    # 2. Clear it
    _direction_hold_clear(imei)
    assert _direction_hold_get(imei) is None

def test_classify_direction():
    now = datetime.now(timezone.utc)
    imei = "TEST_DIR"
    _direction_hold_clear(imei)
    
    # 1. Inside facility -> at_awaso
    d = _classify_direction(
        imei=imei, inside=AWASO, near_facility=None, near_dist=999,
        speed=0, heading=0, gps_fresh=True, last_event_state=None,
        axis=AXIS, lat=6.2, lng=-2.2, now_dt=now
    )
    assert d == "at_awaso"

    # 2. Near facility (4km) and moving -> heading_to_awaso
    d = _classify_direction(
        imei=imei, inside=None, near_facility=AWASO, near_dist=4.0,
        speed=40, heading=0, gps_fresh=True, last_event_state=None,
        axis=AXIS, lat=6.2, lng=-2.0, now_dt=now
    )
    assert d == "heading_to_awaso"

    # 3. Moving on axis -> heading_to_takoradi
    d = _classify_direction(
        imei=imei, inside=None, near_facility=None, near_dist=999,
        speed=40, heading=225, gps_fresh=True, last_event_state=None,
        axis=AXIS, lat=5.5, lng=-2.0, now_dt=now
    )
    assert d == "heading_to_takoradi"

def test_resolve_yard_membership():
    # Fresh GPS says outside, stale alarm says inside -> return None
    alarm = {"key": "waiting", "since": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
    res = _resolve_yard_membership(
        alarm_state=alarm,
        gps_yard=None,
        gps_membership_ok=True,
        gps_time=datetime.now(timezone.utc), # GPS is newer
    )
    assert res is None