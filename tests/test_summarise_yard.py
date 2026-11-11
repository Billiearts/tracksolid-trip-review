import pytest
from datetime import datetime, timezone, timedelta
from report_analysis import summarise_yard, FMT, GBC_GEOIDS

T0 = datetime(2026, 11, 9, 10, 0, 0, tzinfo=timezone.utc)
GBC_GEOID = "791a3bb604ef41cfa302e5dc8f2c356c"

def dt(offset_s):
    return (T0 + timedelta(seconds=offset_s)).strftime(FMT)

def gbc_alarm(event, offset_s):
    """GBC trip-start/end alarms carry the GBC geoid."""
    return {
        "alarmTypeName": f"{'Enter' if event == 'in' else 'Exit'} geo-fence(TRIP TIME GBC)",
        "alertTypeId": event,
        "alertTime": dt(offset_s),
        "geoid": GBC_GEOID,
    }

def bracket_alarm(event, offset_s):
    """GBC EXIT/EXITED bracket fence."""
    return {
        "alarmTypeName": f"{'Enter' if event == 'in' else 'Exit'} geo-fence(GBC EXIT)",
        "alertTypeId": event,
        "alertTime": dt(offset_s),
    }

def station_alarm(name, event, offset_s):
    return {
        "alarmTypeName": f"{'Enter' if event == 'in' else 'Exit'} geo-fence({name})",
        "alertTypeId": event,
        "alertTime": dt(offset_s),
    }

def test_no_visit():
    assert summarise_yard([], T0) is None

def test_full_visit():
    """A visit bracketed by a GBC EXIT out -> next GBC in,
    with maintenance and fueling stops inside."""
    alarms = [
        # Trip 1: out (leaving yard), then trip 2 in (3 hours later)
        gbc_alarm("in", -3600),      # previous trip ends, truck enters yard
        bracket_alarm("in", -3600),  # ...but bracket bracket
        gbc_alarm("out", 0),          # truck leaves yard at T0
        gbc_alarm("in", 10800),       # comes back 3h later (yard visit ends)

        # Inside the visit window [0, 10800]:
        station_alarm("MAINTENANCE TIME", "in", 300),
        station_alarm("MAINTENANCE TIME", "out", 1500),  # 20 min
        station_alarm("FUELING TIME", "in", 2000),
        station_alarm("FUELING TIME", "out", 2300),      # 5 min
    ]
    trip_start = T0 + timedelta(seconds=10800)
    res = summarise_yard(alarms, trip_start)

    assert res is not None
    assert res["total_seconds"] == 10800
    assert res["operations"]["maintenance"]["total_seconds"] == 1200
    assert res["operations"]["fueling"]["total_seconds"] == 300
    assert res["operations"]["loading"]["total_seconds"] == 0

def test_bogus_interval_dropped():
    """A maintenance interval longer than the visit is dropped."""
    alarms = [
        gbc_alarm("out", 0),
        gbc_alarm("in", 3600),
        station_alarm("MAINTENANCE TIME", "in", 100),
        station_alarm("MAINTENANCE TIME", "out", 5000),  # 4900s > 3600s visit
    ]
    trip_start = T0 + timedelta(seconds=3600)
    res = summarise_yard(alarms, trip_start)
    assert res is not None
    assert res["operations"]["maintenance"]["total_seconds"] == 0