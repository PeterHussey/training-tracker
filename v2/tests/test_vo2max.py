from datetime import date

import pandas as pd
import pytest

from metrics.vo2max import daily_vo2max
from normalize import Activity


def _act(d, sport, vo2):
    return Activity(activity_id=int(d.strftime("%Y%m%d")), sport=sport, date=d, ts_ms=0,
                    distance_m=5000.0, duration_s=1800.0, elapsed_s=1800.0, avg_hr=150.0,
                    max_hr=170.0, zone_s={}, vo2max=vo2)


def test_running_only_and_latest_per_day():
    acts = [
        _act(date(2026, 4, 1), "running", 45.0),
        _act(date(2026, 4, 1), "running", 46.0),
        _act(date(2026, 4, 2), "treadmill", 47.0),   # ignored
    ]
    s = daily_vo2max(acts)
    assert s[pd.Timestamp("2026-04-01")] == pytest.approx(46.0)
    assert len(s) == 1


def test_empty_when_no_running_vo2():
    acts = [_act(date(2026, 4, 2), "treadmill", 47.0)]
    assert daily_vo2max(acts).empty
