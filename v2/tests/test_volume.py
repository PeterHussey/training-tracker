from datetime import date

import pandas as pd
import pytest

from metrics.volume import rolling_4wk, week_over_week_pct, weekly_distance, weekly_duration_hours
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


def test_weekly_distance_running_only():
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0),  # Mon -> week 2026-W15
        _act(date(2026, 4, 8), "running", 5_000.0),
        _act(date(2026, 4, 16), "treadmill", 8_000.0),
        _act(date(2026, 4, 16), "running", 6_000.0),
    ]
    wk = weekly_distance(acts, "running")
    assert wk["2026-W15"] == pytest.approx(15.0)
    assert wk["2026-W16"] == pytest.approx(6.0)


def test_rolling_and_wow():
    wk = pd.Series(
        [10.0, 10.0, 20.0, 20.0, 30.0, 30.0],
        index=["2026-W01", "2026-W02", "2026-W03", "2026-W04", "2026-W05", "2026-W06"],
    )
    r = rolling_4wk(wk)
    assert r.iloc[3] == pytest.approx(15.0)  # (10+10+20+20)/4
    wow = week_over_week_pct(wk)
    assert wow.iloc[2] == pytest.approx(1.0)  # 20 vs 10
    assert pd.isna(wow.iloc[0])


def test_groups_total_spans_sports():
    from metrics.volume import groups

    assert set(groups()) == {"total", "running", "treadmill", "cross"}


def test_weekly_duration_hours_sums_cross_with_no_distance():
    # cross has distance 0 but duration>0; duration volume must still count it
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0, dur_s=3600.0),  # 1h running
        _act(date(2026, 4, 7), "cross", 0.0, dur_s=1800.0),  # 0.5h cross
    ]
    total = weekly_duration_hours(acts, "total")
    # both fall in the same ISO week
    assert total.iloc[0] == pytest.approx(1.5)


def test_weekly_duration_hours_excludes_by_group():
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0, dur_s=3600.0),
        _act(date(2026, 4, 7), "cross", 0.0, dur_s=1800.0),
    ]
    assert weekly_duration_hours(acts, "running").iloc[0] == pytest.approx(1.0)
    assert weekly_duration_hours(acts, "cross").iloc[0] == pytest.approx(0.5)
    assert weekly_duration_hours(acts, "running").iloc[0] + weekly_duration_hours(
        acts, "cross"
    ).iloc[0] == pytest.approx(1.5)
