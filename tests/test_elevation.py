from datetime import date

import pandas as pd
import pytest

from metrics.elevation import daily_elevation_gain, gain_per_km, rolling_28d
from normalize import Activity


def _run(d, dist_m, gain_m):
    return Activity(
        activity_id=int(d.strftime("%Y%m%d")),
        sport="running",
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=3600.0,
        elapsed_s=3600.0,
        avg_hr=None,
        max_hr=None,
        zone_s={},
        ele_gain_m=gain_m,
        ele_loss_m=0.0,
    )


def test_daily_and_rolling():
    acts = [_run(date(2026, 4, 1), 10_000.0, 100.0), _run(date(2026, 4, 2), 5000.0, 50.0)]
    daily = daily_elevation_gain(acts, "running")
    assert daily.get(pd.Timestamp("2026-04-02")) == pytest.approx(50.0)
    r = rolling_28d(daily)
    assert r.get(pd.Timestamp("2026-04-02")) == pytest.approx(150.0)


def test_gain_per_km():
    acts = [_run(date(2026, 4, 1), 10_000.0, 100.0)]
    gp = gain_per_km(acts, "running")
    assert gp.iloc[0] == pytest.approx(10.0)  # 100 m / 10 km


def test_indoor_activities_excluded():
    acts = [
        Activity(
            activity_id=1,
            sport="treadmill",
            date=date(2026, 4, 1),
            ts_ms=0,
            distance_m=8000.0,
            duration_s=3600.0,
            elapsed_s=3600.0,
            avg_hr=None,
            max_hr=None,
            zone_s={},
            ele_gain_m=None,
        )
    ]
    daily = daily_elevation_gain(acts, "running")
    assert daily.empty
