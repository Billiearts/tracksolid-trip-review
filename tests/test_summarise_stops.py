import pytest
from datetime import datetime, timezone, timedelta
from report_analysis import summarise_stops, MIN_STOP_SECONDS, MIN_REST_SECONDS

FMT = "%Y-%m-%d %H:%M:%S"
T0 = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)

def dt(offset_s):
    return (T0 + timedelta(seconds=offset_s)).strftime(FMT)

def test_empty():
    res = summarise_stops([], [])
    assert res["count"] == 0
    assert res["total_seconds"] == 0
    assert res["rest_break_count"] == 0

def test_track_only_stop():
    # Stationary for 10 minutes
    track = [
        {"gpsTime": dt(0), "speed": "0", "ignition": "OFF"},
        {"gpsTime": dt(300), "speed": "0", "ignition": "OFF"},
        {"gpsTime": dt(600), "speed": "0", "ignition": "OFF"},
    ]
    res = summarise_stops(track, [], T0, T0 + timedelta(hours=1))
    assert res["count"] == 1
    assert res["total_seconds"] == 600
    assert res["intervals"][0]["seconds"] == 600

def test_parking_and_track_deduped():
    # Same stop in both sources
    track = [
        {"gpsTime": dt(0), "speed": "0", "ignition": "OFF"},
        {"gpsTime": dt(600), "speed": "0", "ignition": "OFF"},
    ]
    parking = [
        {"start": T0, "end": T0 + timedelta(seconds=600), "seconds": 600, "address": "Test"},
    ]
    res = summarise_stops(track, parking, T0, T0 + timedelta(hours=1))
    # Should be 1, not 2
    assert res["count"] == 1
    assert res["intervals"][0]["address"] == "Test" # parking data wins

def test_short_stop_dropped():
    track = [
        {"gpsTime": dt(0), "speed": "0", "ignition": "OFF"},
        {"gpsTime": dt(60), "speed": "0", "ignition": "OFF"},
    ]
    res = summarise_stops(track, [], T0, T0 + timedelta(hours=1))
    assert res["count"] == 0

def test_rest_break_counted():
    # 2-hour stop
    track = [
        {"gpsTime": dt(0), "speed": "0", "ignition": "OFF"},
        {"gpsTime": dt(7200), "speed": "0", "ignition": "OFF"},
    ]
    res = summarise_stops(track, [], T0, T0 + timedelta(hours=3))
    assert res["count"] == 1
    assert res["rest_break_count"] == 1
    assert res["rest_break_seconds"] == 7200