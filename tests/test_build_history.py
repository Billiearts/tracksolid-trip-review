import pytest
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from report_analysis import build_history

# Mock the heavy API calls inside build_history
@patch("report_analysis.fetch_parking")
@patch("report_analysis.fetch_track")
@patch("report_analysis.fetch_alarms_chunked")
def test_history_aggregation(mock_alarms, mock_track, mock_parking):
    # Set up mock alarms for two trips
    ref = datetime(2026, 11, 9, tzinfo=timezone.utc)
    mock_alarms.return_value = [
        # Trip 1
        {"alarmTypeName": "Enter geo-fence(TRIP TIME GBC)", "alertTypeId": "in", "alertTime": "2026-11-01 10:00:00", "geoid": "791a3bb604ef41cfa302e5dc8f2c356c"},
        {"alarmTypeName": "Exit geo-fence(TRIP TIME GBC)", "alertTypeId": "out", "alertTime": "2026-11-01 12:00:00", "geoid": "791a3bb604ef41cfa302e5dc8f2c356c"},
        # Trip 2
        {"alarmTypeName": "Enter geo-fence(TRIP TIME GBC)", "alertTypeId": "in", "alertTime": "2026-11-02 10:00:00", "geoid": "791a3bb604ef41cfa302e5dc8f2c356c"},
        {"alarmTypeName": "Exit geo-fence(TRIP TIME GBC)", "alertTypeId": "out", "alertTime": "2026-11-02 14:00:00", "geoid": "791a3bb604ef41cfa302e5dc8f2c356c"},
        # Event 1
        {"alarmTypeName": "Sudden Acceleration Alert", "alertTypeId": "suddenAcceleration", "alertTime": "2026-11-01 10:30:00", "speed": "40"},
        # Event 2
        {"alarmTypeName": "Sudden Acceleration Alert", "alertTypeId": "suddenAcceleration", "alertTime": "2026-11-02 11:30:00", "speed": "50"},
    ]
    
    # Mock track and parking to just return empty/simple so it doesn't crash
    mock_track.return_value = []
    mock_parking.return_value = []

    res = build_history("123", ref, alarms=mock_alarms.return_value)
    
    assert res["trip_count"] == 2
    # Check event aggregation
    assert res["behaviours"]["hard_acceleration"]["total_events"] == 2
    assert res["behaviours"]["hard_acceleration"]["max_speed"] == 50.0
    # Average max speed: (40 + 50) / 2 = 45
    assert res["behaviours"]["hard_acceleration"]["avg_max_speed"] == 45.0