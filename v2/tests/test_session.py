# tests/test_session.py
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from normalize import from_summary
from profile import default_profile
from session import build_session_view, in_period, week_start, window_series

FIXTURES = Path(__file__).parent / "fixtures"


def _acts():
    raw = json.loads((FIXTURES / "activities_sample.json").read_text())
    return [from_summary(a) for a in raw]


def _lt():
    return json.loads((FIXTURES / "lactate_threshold.json").read_text())


def _race():
    return json.loads((FIXTURES / "race_predictions.json").read_text())


def _view(**kw):
    profile = kw.pop("profile", default_profile(age=40, hrrest=60, sex="M"))
    return build_session_view(kw.pop("acts", _acts()), profile,
                              _lt(), _race())


REQUIRED = {
    "volume.distance_total", "volume.rolling4wk_total", "load.banister",
    "load.edwards", "pmc.ctl", "pmc.atl", "pmc.tsb",
    "load.banister_cross", "load.edwards_cross", "fitness.vo2max",
    "load.cs_approx", "load.lt_hr", "load.lt_pace",
    "race_5k", "race_10k", "race_half", "race_full",
}


def test_session_view_emits_expected_metrics():
    view = _view()
    assert REQUIRED <= set(view.metrics)


def test_series_have_sorted_datetime_index():
    view = _view()
    for metric in view.metrics:
        s = view.series[metric]
        assert isinstance(s.index, pd.DatetimeIndex)
        assert s.index.is_monotonic_increasing


def test_weekly_volume_indexed_to_monday():
    view = _view()
    s = view.series["volume.distance_total"]
    assert (s.index.dayofweek == 0).all()
    assert view.context["volume.distance_total"]["params"]["agg"] == "iso_week"


def test_context_carries_params_and_flags():
    view = _view()
    ban = view.context["load.banister"]
    assert ban["params"]["hrmax"] == 180
    assert ban["flags"]["hrmax_source"] == "age_predicted"
    assert ban["source"] == "computed"
    vo2 = view.context["fitness.vo2max"]
    assert vo2["flags"]["error_class"] == "firstbeat_estimate_5pct"
    cross = view.context["load.banister_cross"]
    assert cross["flags"]["basis"] == "cross_training"


def test_windowed_filters_daily_series():
    view = _view()
    since, until = date(2026, 8, 20), date(2026, 8, 27)
    w = view.windowed(since, until)
    s = w["load.banister"]
    assert (s.index >= pd.Timestamp(since)).all()
    assert (s.index <= pd.Timestamp(until)).all()


def test_windowed_includes_week_overlapping_since():
    # 2026-W34 starts Mon 2026-08-17 and ends Sun 2026-08-23; since=08-20 is
    # inside it, so the row must be kept despite a Monday < since.
    view = _view()
    since, until = date(2026, 8, 20), date(2026, 8, 30)
    s = view.windowed(since, until)["volume.distance_total"]
    assert pd.Timestamp(week_start("2026-W34")) in s.index  # 2026-08-17
    assert pd.Timestamp(week_start("2026-W33")) not in s.index  # 2026-08-10


def test_higher_hrmax_lowers_banister_trimp():
    view_low = _view(profile=default_profile(age=40, hrrest=60, sex="M"))   # hrmax 180
    view_high = _view(profile=default_profile(age=25, hrrest=60, sex="M"))  # hrmax 195
    low, high = view_low.series["load.banister"], view_high.series["load.banister"]
    common = low.index.intersection(high.index)
    day = common[0]
    assert high.loc[day] < low.loc[day]


def test_empty_activities():
    view = build_session_view([], default_profile(age=40, hrrest=60, sex="M"))
    assert view.metrics == []


@pytest.mark.parametrize("label,start", [("2026-W34", date(2026, 8, 17)),
                                         ("2026-W33", date(2026, 8, 10))])
def test_week_start_parse(label, start):
    assert week_start(label) == start


def test_in_period_shared_semantics():
    since = date(2026, 7, 30)
    assert in_period("2026-08-20", since)
    assert not in_period("2026-07-01", since)
    assert in_period("2026-W34", since)
    assert not in_period("2026-W25", since)


def test_window_series_weekly_flag():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-10"), pd.Timestamp("2026-08-17")])
    s = pd.Series([1.0, 2.0], index=idx)
    out = window_series(s, date(2026, 8, 20), date(2026, 8, 30), weekly=True)
    assert list(out.index) == [pd.Timestamp("2026-08-17")]
    assert list(out.values) == [2.0]
