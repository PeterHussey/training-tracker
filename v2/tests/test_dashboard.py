"""Tests for dashboard refresh window logic.

Root cause of the recurring timeout: `refresh_garmin` re-paginated the entire
365-day history every run (~85 100-row pages, >90s) instead of only new pages.
These tests pin the incremental-fetch behavior: subsequent refreshes narrow the
Garmin window to activities newer than the latest already in the local store,
while the first refresh (empty store) keeps the full window.
"""
from datetime import date, timedelta
from unittest import mock

import dashboard as d
from dashboard import FETCH_DAYS, compute_fetch_start


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
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    acts, lt, race = d.refresh_garmin()

    args = fake_gw.fetch_activities.call_args.args
    assert args[0] == expected_start.isoformat()  # narrowed start
    assert args[1] == end.isoformat()
    assert len(acts) == 1


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
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: fake_gw)

    d.refresh_garmin()
    args = fake_gw.fetch_activities.call_args.args
    assert args[0] == (end - timedelta(days=FETCH_DAYS)).isoformat()
