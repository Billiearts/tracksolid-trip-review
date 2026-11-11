import pytest
from report_analysis import summarise_events, SPEED_LIMIT_KPH

def test_hard_acceleration_various_formats():
    """Test all the ways Tracksolid can report hard acceleration."""
    alarms = [
        {"alertTypeId": "suddenAcceleration", "alarmTypeName": "Sudden Acceleration Alert", "speed": "45", "alertTime": "2026-11-09 14:15:52"},
        {"alertTypeId": "41", "alarmTypeName": "Sudden Acceleration", "speed": "40", "alertTime": "2026-11-09 14:16:00"},
        {"alertTypeId": "other", "alarmTypeName": "Harsh Acceleration", "speed": "50", "alertTime": "2026-11-09 14:17:00"},
        {"alertTypeId": "other", "alarmTypeName": "Hard accel", "speed": "42", "alertTime": "2026-11-09 14:18:00"},
    ]
    result = summarise_events(alarms)
    assert result["counts"]["hard_acceleration"] == 4
    assert result["max_speed"]["hard_acceleration"] == 50.0
    assert len(result["events"]) == 4
    assert all(e["kind"] == "hard_acceleration" for e in result["events"])

def test_hard_braking_various_formats():
    alarms = [
        {"alertTypeId": "suddenDeceleration", "alarmTypeName": "Sudden Deceleration Alert", "speed": "30", "alertTime": "2026-11-09 14:15:52"},
        {"alertTypeId": "48", "alarmTypeName": "Deceleration", "speed": "25", "alertTime": "2026-11-09 14:16:00"},
        {"alertTypeId": "other", "alarmTypeName": "Harsh Braking", "speed": "35", "alertTime": "2026-11-09 14:17:00"},
    ]
    result = summarise_events(alarms)
    assert result["counts"]["hard_braking"] == 3
    assert result["max_speed"]["hard_braking"] == 35.0

def test_hard_cornering_various_formats():
    alarms = [
        {"alertTypeId": "sharpTurnLeft", "alarmTypeName": "Sharp Turn Left Alert", "speed": "40", "alertTime": "2026-11-09 14:15:52"},
        {"alertTypeId": "sharpTurnRight", "alarmTypeName": "Sharp Turn Right Alert", "speed": "45", "alertTime": "2026-11-09 14:16:00"},
        {"alertTypeId": "43", "alarmTypeName": "Cornering", "speed": "38", "alertTime": "2026-11-09 14:17:00"},
    ]
    result = summarise_events(alarms)
    assert result["counts"]["hard_cornering"] == 3
    assert result["max_speed"]["hard_cornering"] == 45.0

def test_speeding_buckets():
    alarms = [
        # Speed limit is 75
        {"alertTypeId": "overSpeed", "alarmTypeName": "Overspeed", "speed": "80", "alertTime": "2026-11-09 14:15:00"}, # <10 over
        {"alertTypeId": "overSpeed", "alarmTypeName": "Overspeed", "speed": "90", "alertTime": "2026-11-09 14:16:00"}, # <20 over
        {"alertTypeId": "overSpeed", "alarmTypeName": "Overspeed", "speed": "100", "alertTime": "2026-11-09 14:17:00"}, # >20 over
        {"alertTypeId": "other", "alarmTypeName": "Over speed", "speed": "82", "alertTime": "2026-11-09 14:18:00"},
    ]
    result = summarise_events(alarms)
    assert result["counts"]["speeding_10"] == 2
    assert result["counts"]["speeding_20"] == 1
    assert result["counts"]["speeding_over_20"] == 1
    assert result["max_speed"]["speeding_over_20"] == 100.0

def test_geofence_alarms_ignored():
    """Geofence events should not be counted as driver behaviour."""
    alarms = [
        {"alertTypeId": "in", "alarmTypeName": "Enter geo-fence(GBC)", "speed": "0", "alertTime": "2026-11-09 14:15:00"},
        {"alertTypeId": "out", "alarmTypeName": "Exit geo-fence(GBC)", "speed": "0", "alertTime": "2026-11-09 14:16:00"},
    ]
    result = summarise_events(alarms)
    assert sum(result["counts"].values()) == 0
    assert len(result["events"]) == 0

def test_coordinates_extracted():
    alarms = [
        {"alertTypeId": "suddenAcceleration", "alarmTypeName": "Accel", "speed": "45", "alertTime": "2026-11-09 14:15:52", "lat": 5.5, "lng": -1.2},
    ]
    result = summarise_events(alarms)
    e = result["events"][0]
    assert e["lat"] == 5.5
    assert e["lng"] == -1.2
    assert e["time"] == "2026-11-09 14:15:52"