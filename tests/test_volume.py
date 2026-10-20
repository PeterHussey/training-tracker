from datetime import date

import pandas as pd
import pytest

from metrics.volume import (
    bucket_span,
    daily_distance,
    daily_duration_hours,
    groups,
    resample_agg,
    rolling_4wk,
    week_over_week_pct,
)
from normalize import Activity


def _act(d: date, sport: str, dist_m: float, dur_s: float = 3600.0) -> Activity:
    return Activity(
        activity_id=int(d.strftime("%Y%m%d")) + hash(sport) % 1000,
        sport=sport,
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=dur_s,
        elapsed_s=dur_s,
        avg_hr=None,
        max_hr=None,
        zone_s={},
        ele_gain_m=0.0,
    )


def test_daily_distance_running_only():
    """daily_distance includes only running + treadmill (distance is meaningless
    for cross/strength)."""
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0),  # 10 km
        _act(date(2026, 4, 8), "running", 5_000.0),  # 5 km
        _act(date(2026, 4, 16), "treadmill", 8_000.0),  # 8 km
        _act(date(2026, 4, 16), "cross", 0.0),  # cross excluded
        _act(date(2026, 4, 17), "strength", 0.0),  # strength excluded
    ]
    s = daily_distance(acts, "total")
    assert len(s) == 3  # three distinct days with running/treadmill
    assert s.loc[pd.Timestamp("2026-04-06")] == pytest.approx(10.0)
    assert s.loc[pd.Timestamp("2026-04-08")] == pytest.approx(5.0)
    assert s.loc[pd.Timestamp("2026-04-16")] == pytest.approx(8.0)


def test_daily_duration_includes_cross():
    """daily_duration_hours includes cross/strength — duration is real even
    when distance is zero."""
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0, dur_s=3600.0),  # 1 h
        _act(date(2026, 4, 7), "cross", 0.0, dur_s=1800.0),  # 0.5 h
        _act(date(2026, 4, 8), "strength", 0.0, dur_s=2700.0),  # 0.75 h
    ]
    s = daily_duration_hours(acts, "total")
    assert len(s) == 3
    assert s.loc[pd.Timestamp("2026-04-06")] == pytest.approx(1.0)
    assert s.loc[pd.Timestamp("2026-4-07")] == pytest.approx(0.5)
    assert s.loc[pd.Timestamp("2026-04-08")] == pytest.approx(0.75)


def test_daily_multiple_same_day_summed():
    """Two activities on the same day are summed."""
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0, dur_s=3600.0),
        _act(date(2026, 4, 6), "treadmill", 5_000.0, dur_s=1800.0),
    ]
    dist = daily_distance(acts, "total")
    dur = daily_duration_hours(acts, "total")
    assert len(dist) == 1
    assert dist.iloc[0] == pytest.approx(15.0)
    assert dur.iloc[0] == pytest.approx(1.0 + 0.5)


def test_resample_weekly_matches_iso_total():
    """resample_agg(daily, 'W-SUN') groups Mon–Sun like ISO week, labels
    each week by its Monday, and sums to match the old weekly_distance."""
    acts = [
        # Mon 2026-04-06: 10 km running
        _act(date(2026, 4, 6), "running", 10_000.0),
        # Wed 2026-04-08: 5 km running
        _act(date(2026, 4, 8), "running", 5_000.0),
        # Thu 2026-04-16: 8 km treadmill
        _act(date(2026, 4, 16), "treadmill", 8_000.0),
    ]
    daily = daily_distance(acts, "total")
    weekly = resample_agg(daily, "W-SUN")
    # Mon Apr 6 – Sun Apr 12: 10 + 5 = 15
    # Mon Apr 13 – Sun Apr 19: 8
    assert len(weekly) == 2
    # Index is shifted to Monday (ISO-week convention)
    apr6_idx = pd.Timestamp("2026-04-06")
    apr13_idx = pd.Timestamp("2026-04-13")
    assert weekly.loc[apr6_idx] == pytest.approx(15.0)
    assert weekly.loc[apr13_idx] == pytest.approx(8.0)


def test_resample_monthly():
    """resample_agg with 'MS' sums to month-start-labelled buckets."""
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0),
        _act(date(2026, 4, 20), "running", 5_000.0),
        _act(date(2026, 5, 3), "running", 8_000.0),
    ]
    daily = daily_distance(acts, "total")
    monthly = resample_agg(daily, "MS")
    assert len(monthly) == 2
    apr1 = pd.Timestamp("2026-04-01")
    may1 = pd.Timestamp("2026-05-01")
    assert monthly.loc[apr1] == pytest.approx(15.0)
    assert monthly.loc[may1] == pytest.approx(8.0)


def test_bucket_span_covers_whole_bucket():
    """bucket_span gives the days each label covers: Mon–Sun for W-SUN,
    1st–last for MS."""
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-17"), pd.Timestamp("2026-08-01")])
    starts, ends = bucket_span(idx, "W-SUN")
    assert starts.iloc[0] == pd.Timestamp("2026-08-17")
    assert ends.iloc[0] == pd.Timestamp("2026-08-23")
    starts, ends = bucket_span(idx, "MS")
    assert starts.iloc[1] == pd.Timestamp("2026-08-01")
    assert ends.iloc[1] == pd.Timestamp("2026-08-31")


def test_empty_series():
    """Empty input → empty series, no crash."""
    d = daily_distance([], "total")
    assert len(d) == 0
    assert d.dtype == float

    du = daily_duration_hours([], "total")
    assert len(du) == 0
    assert du.dtype == float

    r = resample_agg(pd.Series([], dtype=float), "W-SUN")
    assert len(r) == 0


def test_daily_series_name():
    """Daily series have readable .name attributes."""
    acts = [_act(date(2026, 4, 6), "running", 10_000.0)]
    assert daily_distance(acts, "total").name == "distance_km"
    assert daily_duration_hours(acts, "total").name == "duration_hours"


def test_rolling_and_wow():
    wk = pd.Series(
        [10.0, 10.0, 20.0, 20.0, 30.0, 30.0],
        index=pd.to_datetime(
            ["2026-01-05", "2026-01-12", "2026-01-19", "2026-01-26", "2026-02-02", "2026-02-09"]
        ),
    )
    r = rolling_4wk(wk)
    assert r.iloc[3] == pytest.approx(15.0)  # (10+10+20+20)/4
    wow = week_over_week_pct(wk)
    assert wow.iloc[2] == pytest.approx(1.0)  # 20 vs 10
    assert pd.isna(wow.iloc[0])


def test_groups_total_spans_sports():
    assert set(groups()) == {"total", "running", "treadmill", "cross", "strength"}
