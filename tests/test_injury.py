from datetime import date

import pandas as pd
import pytest

from metrics.injury import longest_run_30d, max_run_ratio
from normalize import Activity


def _act(d: date, sport: str, dist_m: float) -> Activity:
    return Activity(
        activity_id=int(d.strftime("%Y%m%d")) + hash(sport) % 1000,
        sport=sport,
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=3600.0,
        elapsed_s=3600.0,
        avg_hr=None,
        max_hr=None,
        zone_s={},
    )


class TestLongestRun30d:
    def test_empty_returns_empty(self):
        assert longest_run_30d([]).empty

    def test_single_run(self):
        s = longest_run_30d([_act(date(2026, 8, 1), "running", 10_000)])
        assert len(s) == 1
        assert s.iloc[0] == pytest.approx(10.0)

    def test_rolling_max_picks_longest(self):
        acts = [
            _act(date(2026, 8, 1), "running", 5_000),
            _act(date(2026, 8, 5), "running", 15_000),
            _act(date(2026, 8, 10), "running", 8_000),
        ]
        s = longest_run_30d(acts)
        assert s[pd.Timestamp("2026-08-01")] == pytest.approx(5.0)
        assert s[pd.Timestamp("2026-08-05")] == pytest.approx(15.0)
        assert s[pd.Timestamp("2026-08-10")] == pytest.approx(15.0)

    def test_treadmill_included(self):
        acts = [
            _act(date(2026, 8, 1), "running", 5_000),
            _act(date(2026, 8, 2), "treadmill", 12_000),
        ]
        s = longest_run_30d(acts)
        assert s[pd.Timestamp("2026-08-02")] == pytest.approx(12.0)

    def test_strength_does_not_affect_rolling_max(self):
        # Strength activity on day 2 does not contribute to running max
        acts = [
            _act(date(2026, 8, 1), "running", 5_000),
            _act(date(2026, 8, 2), "strength", 50_000),
        ]
        s = longest_run_30d(acts)
        # Day 2 max is still 5km (only running counts), day 2 not in index
        # because strength-only days don't create rolling max entries
        assert s[pd.Timestamp("2026-08-01")] == pytest.approx(5.0)
        assert pd.Timestamp("2026-08-02") not in s.index

    def test_cross_does_not_affect_rolling_max(self):
        acts = [
            _act(date(2026, 8, 1), "running", 5_000),
            _act(date(2026, 8, 2), "cross", 50_000),
        ]
        s = longest_run_30d(acts)
        assert s[pd.Timestamp("2026-08-01")] == pytest.approx(5.0)
        assert pd.Timestamp("2026-08-02") not in s.index


class TestMaxRunRatio:
    def test_empty_returns_empty(self):
        assert max_run_ratio([]).empty

    def test_baseline_guard(self):
        # Only 3 running days — below min_baseline=7, so no ratio emitted
        acts = [_act(date(2026, 8, i), "running", 5_000) for i in range(1, 4)]
        assert max_run_ratio(acts).empty

    def test_ratio_one_when_all_equal(self):
        # 10 identical runs. With shifted max (prior 30d), days 1-7 have
        # <7 prior running days so are skipped. Days 8-10 have >=7 prior
        # days and ratio = 5/5 = 1.0.
        acts = [_act(date(2026, 8, i), "running", 5_000) for i in range(1, 11)]
        s = max_run_ratio(acts)
        assert len(s) == 3  # days 8, 9, 10
        assert all(v == pytest.approx(1.0) for v in s.values)

    def test_ratio_above_one_for_new_pr(self):
        # 10 days of 5km, then a 10km run on day 11.
        # prior_max on day 11 = max of days 1-10 = 5km, ratio = 10/5 = 2.0.
        acts = [_act(date(2026, 8, i), "running", 5_000) for i in range(1, 11)]
        acts.append(_act(date(2026, 8, 11), "running", 10_000))
        s = max_run_ratio(acts)
        assert s[pd.Timestamp("2026-08-11")] == pytest.approx(2.0)

    def test_ratio_captures_old_long_run(self):
        # Day 1: 10km, days 2-10: 3km each, day 11: 8km
        acts = [_act(date(2026, 8, 1), "running", 10_000)]
        acts += [_act(date(2026, 8, i), "running", 3_000) for i in range(2, 11)]
        acts.append(_act(date(2026, 8, 11), "running", 8_000))
        s = max_run_ratio(acts)
        # Day 11: prior_max = max of days 1-10 = 10km, ratio = 8/10 = 0.8
        assert s[pd.Timestamp("2026-08-11")] == pytest.approx(0.8)

    def test_warning_threshold(self):
        # 10 days of 10km, then a 12km run → prior_max = 10, ratio = 1.2
        acts = [_act(date(2026, 8, i), "running", 10_000) for i in range(1, 11)]
        acts.append(_act(date(2026, 8, 11), "running", 12_000))
        s = max_run_ratio(acts)
        assert s[pd.Timestamp("2026-08-11")] == pytest.approx(1.2)

    def test_danger_threshold(self):
        # 10 days of 10km, then a 15km run → prior_max = 10, ratio = 1.5
        acts = [_act(date(2026, 8, i), "running", 10_000) for i in range(1, 11)]
        acts.append(_act(date(2026, 8, 11), "running", 15_000))
        s = max_run_ratio(acts)
        assert s[pd.Timestamp("2026-08-11")] == pytest.approx(1.5)

    def test_multiple_runs_same_day_takes_worst(self):
        # 10 days of 10km, then two runs on day 11: 5km and 13km
        acts = [_act(date(2026, 8, i), "running", 10_000) for i in range(1, 11)]
        acts.append(_act(date(2026, 8, 11), "running", 5_000))
        acts.append(_act(date(2026, 8, 11), "running", 13_000))
        s = max_run_ratio(acts)
        # max ratio on day 11: 13/10 = 1.3
        assert s[pd.Timestamp("2026-08-11")] == pytest.approx(1.3)
