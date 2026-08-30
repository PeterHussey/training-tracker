from datetime import date

import pandas as pd
import pytest

from metrics.trimp import banister_trimp, daily_trimp, edwards_trimp
from normalize import Activity
from profile import default_profile


def test_edwards_trimp_sum_zone_minutes_times_weight():
    # 60 min in zone 1 repeated 5 times => 60*1*5 = 300
    zone_s = {1: 5 * 3600.0}
    assert edwards_trimp(zone_s) == pytest.approx(300.0)


def test_banister_trimp_male_at_threshold():
    p = default_profile(age=40, hrrest=60, sex="M")  # hrmax 180
    # avg HR 150 => dHR = (150-60)/(180-60) = 0.75
    trimp = banister_trimp(duration_min=30.0, avg_hr=150.0, profile=p)
    expected = 30.0 * 0.75 * 0.64 * (2.71828183 ** (1.92 * 0.75))
    assert trimp == pytest.approx(expected, rel=1e-9)


def test_banister_trimp_female_uses_167():
    p = default_profile(age=40, hrrest=60, sex="F")
    trimp_f = banister_trimp(30.0, 150.0, p)
    trimp_m = banister_trimp(30.0, 150.0, default_profile(age=40, hrrest=60, sex="M"))
    assert trimp_f < trimp_m


def test_banister_trimp_invalid_hrmax():
    p = default_profile(age=40, hrrest=60, sex="M")
    p.hrmax = 60
    with pytest.raises(ValueError):
        banister_trimp(30.0, 150.0, p)


def test_daily_trimp_sums_multiple_activities_per_day():
    acts = [
        Activity(1, "running", date(2026, 4, 1), 0, 10000.0, 3600.0, 3600.0, 140.0, 160.0,
                 zone_s={z: 720.0 for z in range(1, 6)}),   # 12 min/zone -> 12*(1+2+3+4+5)=180
        Activity(2, "running", date(2026, 4, 1), 0, 5000.0, 1800.0, 1800.0, 120.0, 140.0,
                 zone_s={z: 360.0 for z in range(1, 6)}),   # 6 min/zone  -> 6*15=90
    ]
    df = daily_trimp(acts, default_profile(age=40, hrrest=60, sex="M"))
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["edwards"].iloc[0] == pytest.approx(270.0)