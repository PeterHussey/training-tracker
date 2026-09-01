"""Tests for dashboard refresh window logic.

Root cause of the recurring timeout: `refresh_garmin` re-paginated the entire
365-day history every run (~85 100-row pages, >90s) instead of only new pages.
These tests pin the incremental-fetch behavior: subsequent refreshes narrow the
Garmin window to activities newer than the latest already in the local store,
while the first refresh (empty store) keeps the full window.
"""

from datetime import date, timedelta
from unittest import mock

import pytest

import dashboard as d
from dashboard import FETCH_DAYS, compute_fetch_start, DEFAULT_WINDOW_DAYS, period_bounds


def test_compute_fetch_start_full_window_when_empty():
    end = date(2026, 8, 31)
    assert compute_fetch_start(end, None) == end - timedelta(days=FETCH_DAYS)


def test_compute_fetch_start_narrows_after_latest_activity():
    end = date(2026, 8, 31)
    latest = date(2026, 8, 28)
    # only activities from Aug 29 onward
    assert compute_fetch_start(end, latest) == date(2026, 8, 29)


def test_compute_fetch_start_floored_to_fetch_window():
    end = date(2026, 8, 31)
    # a very old latest date must not widen past the FETCH_DAYS floor
    assert compute_fetch_start(end, date(2020, 1, 1)) == end - timedelta(days=FETCH_DAYS)


def test_refresh_garmin_narrows_window_when_store_has_activities(monkeypatch):
    """Subsequent refreshes must only ask Garmin for activities newer than what
    is already persisted locally — this is the regression guard for the volume-
    driven timeout."""
    end = date(2026, 8, 31)
    latest = date(2026, 8, 28)
    expected_start = date(2026, 8, 29)

    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = latest
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.fetch_activities.return_value = [{"activityId": 1}]
    fake_gw.fetch_lactate_threshold.return_value = {"speed_and_heart_rate": {}, "power": {}}
    fake_gw.fetch_race_predictions.return_value = {"maybeMap": {}}
    fake_gw.fetch_vo2max_trend.return_value = [{"generic": {"calendarDate": "2026-08-29"}}]
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    acts, lt, race, vo2 = d.refresh_garmin()

    args = fake_gw.fetch_activities.call_args.args
    assert args[0] == expected_start.isoformat()  # narrowed start
    assert args[1] == end.isoformat()
    assert len(acts) == 1
    # VO2max trend ALWAYS uses the full window, independent of the incremental
    # activities window (single non-paginated daily-summary call).
    vo2_args = fake_gw.fetch_vo2max_trend.call_args.args
    assert vo2_args[0] == (end - timedelta(days=FETCH_DAYS)).isoformat()
    assert vo2_args[1] == end.isoformat()
    assert vo2[0]["generic"]["calendarDate"] == "2026-08-29"


def test_refresh_garmin_full_window_when_store_empty(monkeypatch):
    """First refresh: empty local store => fetch full FETCH_DAYS window."""
    end = date(2026, 8, 31)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = None
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.fetch_activities.return_value = [{"activityId": 1}]
    fake_gw.fetch_lactate_threshold.return_value = {"speed_and_heart_rate": {}, "power": {}}
    fake_gw.fetch_race_predictions.return_value = {"maybeMap": {}}
    fake_gw.fetch_vo2max_trend.return_value = []
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)

    acts, lt, race, vo2 = d.refresh_garmin()
    args = fake_gw.fetch_activities.call_args.args
    assert args[0] == (end - timedelta(days=FETCH_DAYS)).isoformat()
    assert vo2 == []


def test_refresh_garmin_no_activities_is_noop_when_store_has_data(monkeypatch):
    """Incremental refresh with nothing new must be a no-op, not a fatal error.

    `compute_fetch_start` narrows the window to newer-than-latest (Aug 31..Sep 1
    here); when the athlete simply has not run since, Garmin returns [] for that
    window (verified live: garmin_raw.json == []). That is a NORMAL outcome of
    the incremental design — the dashboard must keep existing data and not raise
    "Garmin returned no activities". Trend payloads still refresh.
    """
    end = date(2026, 9, 1)
    latest = date(2026, 8, 30)
    expected_start = date(2026, 8, 31)

    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = latest
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.fetch_activities.return_value = []  # API legitimately empty
    fake_gw.fetch_lactate_threshold.return_value = {"speed_and_heart_rate": {}, "power": {}}
    fake_gw.fetch_race_predictions.return_value = {"maybeMap": {}}
    fake_gw.fetch_vo2max_trend.return_value = [{"generic": {"calendarDate": "2026-08-31"}}]
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    acts, lt, race, vo2 = d.refresh_garmin()

    assert acts == []  # nothing new: no-op, not an exception
    assert vo2[0]["generic"]["calendarDate"] == "2026-08-31"  # trend still refreshed
    args = fake_gw.fetch_activities.call_args.args
    assert args[0] == expected_start.isoformat()
    assert args[1] == end.isoformat()


def test_refresh_garmin_raises_only_when_store_empty(monkeypatch):
    """First-run failure (no local data AND no API data) still surfaces loudly."""
    end = date(2026, 9, 1)
    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = None
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.fetch_activities.return_value = []
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    with pytest.raises(RuntimeError, match="returned no activities"):
        d.refresh_garmin()


def test_period_bounds_min_and_end_from_dates():
    dates = [date(2026, 1, 5), date(2026, 8, 30), date(2026, 3, 10)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 1, 5)
    assert end == date(2026, 8, 30)


def test_period_bounds_trailing_window_when_span_exceeds_window():
    dates = [date(2026, 1, 5), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 1, 5)
    assert since == end - timedelta(days=DEFAULT_WINDOW_DAYS)
    assert end == date(2026, 8, 30)


def test_period_bounds_floored_to_min_when_span_under_window():
    dates = [date(2026, 8, 20), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert min_d == date(2026, 8, 20)
    assert since == date(2026, 8, 20)
    assert end == date(2026, 8, 30)


def test_period_bounds_independent_of_today():
    # Pinning shows the helper never consults the current date.
    dates = [date(2026, 1, 5), date(2026, 8, 30)]
    min_d, since, end = period_bounds(dates)
    assert end == date(2026, 8, 30)
    assert min_d == date(2026, 1, 5)
