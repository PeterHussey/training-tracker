from datetime import date

import pandas as pd
import pytest

from metrics.vo2max import daily_vo2max, daily_vo2max_from_trend
from normalize import Activity


def _act(d, sport, vo2):
    return Activity(
        activity_id=int(d.strftime("%Y%m%d")),
        sport=sport,
        date=d,
        ts_ms=0,
        distance_m=5000.0,
        duration_s=1800.0,
        elapsed_s=1800.0,
        avg_hr=150.0,
        max_hr=170.0,
        zone_s={},
        vo2max=vo2,
    )


def test_running_only_and_latest_per_day():
    acts = [
        _act(date(2026, 4, 1), "running", 45.0),
        _act(date(2026, 4, 1), "running", 46.0),
        _act(date(2026, 4, 2), "treadmill", 47.0),  # ignored
    ]
    s = daily_vo2max(acts)
    assert s[pd.Timestamp("2026-04-01")] == pytest.approx(46.0)
    assert len(s) == 1


def test_empty_when_no_running_vo2():
    acts = [_act(date(2026, 4, 2), "treadmill", 47.0)]
    assert daily_vo2max(acts).empty


def test_trend_uses_precise_value():
    payload = [
        {"generic": {"calendarDate": "2026-08-09", "vo2MaxValue": 46, "vo2MaxPreciseValue": 46.5}},
        {"generic": {"calendarDate": "2026-08-10", "vo2MaxValue": 46, "vo2MaxPreciseValue": 46.2}},
    ]
    s = daily_vo2max_from_trend(payload)
    assert s[pd.Timestamp("2026-08-09")] == pytest.approx(46.5)
    assert s[pd.Timestamp("2026-08-10")] == pytest.approx(46.2)


def test_trend_falls_back_to_integer_value():
    payload = [{"generic": {"calendarDate": "2026-08-09", "vo2MaxValue": 46}}]
    s = daily_vo2max_from_trend(payload)
    assert s[pd.Timestamp("2026-08-09")] == pytest.approx(46.0)


def test_trend_skips_entries_without_vo2():
    payload = [
        {"generic": {"calendarDate": "2026-08-09", "vo2MaxPreciseValue": 46.5}},
        {"generic": {"calendarDate": "2026-08-10"}},
        {"cycling": {"calendarDate": "2026-08-10", "vo2MaxPreciseValue": 40.0}},
    ]
    s = daily_vo2max_from_trend(payload)
    assert len(s) == 1
    assert s.iloc[0] == pytest.approx(46.5)


def test_trend_empty_payload_empty_series():
    assert daily_vo2max_from_trend([]).empty
    assert daily_vo2max_from_trend(None).empty


def test_trend_latest_per_day_when_duplicates():
    payload = [
        {"generic": {"calendarDate": "2026-08-09", "vo2MaxPreciseValue": 46.2}},
        {"generic": {"calendarDate": "2026-08-09", "vo2MaxPreciseValue": 46.5}},
    ]
    s = daily_vo2max_from_trend(payload)
    assert len(s) == 1
    assert s.iloc[0] == pytest.approx(46.5)
