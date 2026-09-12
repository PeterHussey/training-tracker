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
from dashboard import (
    DEFAULT_WINDOW_DAYS,
    FETCH_DAYS,
    compute_fetch_start,
    period_bounds,
    race_time_ticks,
)


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
    fake_gw.fetch_race_predictions_trend.return_value = [
        {"calendarDate": "2026-08-29", "time5K": 1405}
    ]
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
    # Race trend likewise ALWAYS uses the full window (daily history endpoint,
    # not the /latest single snapshot), so the chart has a real time series.
    race_args = fake_gw.fetch_race_predictions_trend.call_args.args
    assert race_args[0] == (end - timedelta(days=FETCH_DAYS)).isoformat()
    assert race_args[1] == end.isoformat()
    assert race[0]["time5K"] == 1405


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
    fake_gw.fetch_race_predictions_trend.return_value = [
        {"calendarDate": "2026-08-29", "time5K": 1405}
    ]
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
    fake_gw.fetch_race_predictions_trend.return_value = [
        {"calendarDate": "2026-08-29", "time5K": 1405}
    ]
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


def test_race_time_ticks_cover_values_as_hmmss():
    vals, texts = race_time_ticks([1413.0, 3100.0, 7460.0, 17267.0])
    assert vals[0] <= 1413.0
    assert vals[-1] >= 17267.0
    assert len(vals) <= 8
    assert texts[0] == "0:30:00" or ":" in texts[0]
    assert all(":" in t for t in texts)
    assert [float(v) for v in vals] == sorted(vals)


def test_race_time_ticks_narrow_span_uses_minutes():
    vals, texts = race_time_ticks([1405.0, 1413.0])
    assert vals[0] <= 1405.0
    assert vals[-1] >= 1413.0
    assert all(":" in t for t in texts)


def test_race_time_ticks_empty():
    assert race_time_ticks([]) == ([], [])


def test_render_fitness_tab_no_rolling_threshes():
    """The old rolling 45-day LT fallback block must be removed.

    Task 5 replaces it with best-effort anchor cards + qualifier dots.
    If this test fails, someone re-introduced the dead code path.
    """
    import inspect

    src = inspect.getsource(d.render_fitness_tab)
    assert "rolling_threshes" not in src, (
        "render_fitness_tab still references rolling_threshes — expected removed by Task 5"
    )


def test_anchor_point_survives_period_windowing():
    """Anchor cards read the unfiltered view: an all-time best that predates
    the selected period must still resolve (dots alone don't count)."""
    import json
    from datetime import date as _date
    from pathlib import Path
    from profile import default_profile

    from dashboard import anchor_point
    from normalize import from_summary
    from session import build_session_view

    raw = json.loads((Path(__file__).parent / "fixtures" / "activities_sample.json").read_text())
    acts = [from_summary(a) for a in raw]
    qual = [
        a for a in acts if a.sport == "running" and a.duration_s >= 1200 and a.avg_hr is not None
    ]
    assert qual, "need a qualifying activity in fixtures"
    target = qual[0]
    n = int(target.duration_s)
    series_by_id = {
        target.activity_id: (
            [165.0] * n,
            [3.5] * n,
            [float(i * 1000) for i in range(n)],
        )
    }
    view = build_session_view(
        acts, default_profile(age=40, hrrest=60, sex="M"), None, None, None, series_by_id
    )
    assert "load.lt_hr_best20" in view.series
    anchor_date = view.series["load.lt_hr_best20"].index[0].date()
    # A window starting after the anchor date drops the single anchor point...
    w = view.windowed(anchor_date + timedelta(days=1), _date(2026, 9, 6))
    assert w.get("load.lt_hr_best20") is None or w["load.lt_hr_best20"].empty
    # ...but the card helper resolves it from the full view anyway.
    pt = anchor_point(view, "load.lt_hr_best20", "load.lt_pace_best20")
    assert pt is not None
    assert pt["proxy_hr"] == pytest.approx(165.0 * 0.95)
    assert pt["date"] == anchor_date.isoformat()


def test_windowed_view_covers_all_expected_metrics():
    """Criterion 3 proxy: windowing must apply uniformly to every metric in the
    view. If a metric is present in the full view but missing from the windowed
    output when its data falls inside the window, the dashboard chart would show
    stale or empty data."""
    import json
    from pathlib import Path
    from profile import default_profile

    from normalize import from_summary
    from session import build_session_view

    raw = json.loads((Path(__file__).parent / "fixtures" / "activities_sample.json").read_text())
    acts = [from_summary(a) for a in raw]
    profile = default_profile(age=40, hrrest=60, sex="M")
    view = build_session_view(acts, profile, None, None, None)

    # Window covering the full fixture date range
    dates = [a.date for a in acts]
    since, until = min(dates), max(dates)
    windowed = view.windowed(since, until)

    # Every metric present in the full view must also appear in the windowed
    # view (possibly empty if all its data points fall outside the window, but
    # the key must exist so the dashboard chart renders without KeyError).
    full_metrics = set(view.series.keys())
    windowed_metrics = set(windowed.keys())
    assert full_metrics == windowed_metrics, (
        f"Metrics in full view but missing from windowed: {full_metrics - windowed_metrics}"
    )


def test_build_repetitions_groups_by_code():
    """Criterion 4 proxy: build_repetitions must group activities by their 80/20
    plan code, exclude activities without a code, and sort each group oldest-first."""
    from datetime import date as _date

    from normalize import Activity
    from session import build_repetitions

    acts = [
        Activity(activity_id=1, sport="running", date=_date(2026, 8, 10), ts_ms=0,
                 distance_m=5000, duration_s=1800, elapsed_s=1800, zone_s={},
                 ele_gain_m=50, vo2max=None, avg_hr=150, max_hr=170,
                 aerobic_te=None, anaerobic_te=None, avg_speed=2.78,
                 fastest_split_1609=None, name="Run - RF24 (Foundation)", has_intervals=False),
        Activity(activity_id=2, sport="running", date=_date(2026, 8, 17), ts_ms=0,
                 distance_m=5000, duration_s=1750, elapsed_s=1750, zone_s={},
                 ele_gain_m=40, vo2max=None, avg_hr=152, max_hr=172,
                 aerobic_te=None, anaerobic_te=None, avg_speed=2.86,
                 fastest_split_1609=None, name="Run - RF24 (Foundation)", has_intervals=False),
        Activity(activity_id=3, sport="running", date=_date(2026, 8, 12), ts_ms=0,
                 distance_m=8000, duration_s=3000, elapsed_s=3000, zone_s={},
                 ele_gain_m=30, vo2max=None, avg_hr=145, max_hr=165,
                 aerobic_te=None, anaerobic_te=None, avg_speed=2.67,
                 fastest_split_1609=None, name="Easy run - no code", has_intervals=False),
        Activity(activity_id=4, sport="treadmill", date=_date(2026, 8, 14), ts_ms=0,
                 distance_m=5000, duration_s=1900, elapsed_s=1900, zone_s={},
                 ele_gain_m=0, vo2max=None, avg_hr=148, max_hr=168,
                 aerobic_te=None, anaerobic_te=None, avg_speed=2.63,
                 fastest_split_1609=None, name="Treadmill - RF24 (Foundation)", has_intervals=False),
    ]
    groups = build_repetitions(acts)
    # Only activities with code "RF24" are grouped
    assert "RF24" in groups
    assert len(groups["RF24"]) == 3  # ids 1, 2, 4 (treadmill has code too)
    # Activities without a code are excluded
    assert all(a.activity_id != 3 for a in groups["RF24"])
    # Sorted oldest-first
    assert groups["RF24"][0].date < groups["RF24"][-1].date
    # No other codes in the group
    assert len(groups) == 1


def _long_run(activity_id, day, dur_s=6300.0, avg_speed=3.0, intervals=False):
    from normalize import Activity

    return Activity(
        activity_id=activity_id,
        sport="running",
        date=day,
        ts_ms=0,
        distance_m=18000.0,
        duration_s=dur_s,
        elapsed_s=dur_s,
        avg_hr=150.0,
        max_hr=170.0,
        zone_s={},
        ele_gain_m=100.0,
        lat=42.4261,
        lon=-71.2801,
        avg_speed=avg_speed,
        has_intervals=intervals,
    )


def _cached_details(cache_dir, activity_id, minutes=104):
    import json as _json

    half = minutes // 2
    rows = []
    for i in range(minutes):
        hr = 145.0 if i < half else 155.0
        rows.append({"metrics": [hr, 3.0, i * 60000]})
    payload = {
        "metricDescriptors": [
            {"key": "directHeartRate", "metricsIndex": 0},
            {"key": "directSpeed", "metricsIndex": 1},
            {"key": "directTimestamp", "metricsIndex": 2},
        ],
        "activityDetailMetrics": rows,
    }
    (cache_dir / f"activity_details_{activity_id}.json").write_text(_json.dumps(payload))


def test_decoupling_fetch_pass_covers_long_slow_runs(tmp_path):
    """The LTHR details pass takes the top-40 fastest runs, which misses slow
    long runs. The decoupling pass must select by duration/recency instead."""
    from dashboard import fetch_details_for_decoupling

    slow_long = _long_run(501, date(2026, 5, 10), avg_speed=2.5)
    fast_short = _long_run(502, date(2026, 5, 11), dur_s=1800.0, avg_speed=4.5)
    interval_long = _long_run(503, date(2026, 5, 12), intervals=True)
    _cached_details(tmp_path, 501)
    _cached_details(tmp_path, 502)

    class _Gw:
        cache_dir = tmp_path

        def fetch_activity_details(self, activity_id):
            raise AssertionError("must hit cache, not network")

    series = fetch_details_for_decoupling(
        _Gw(),  # type: ignore[arg-type]
        [slow_long, fast_short, interval_long],
        since=date(2026, 5, 1),
        until=date(2026, 5, 31),
    )
    assert 501 in series and series[501][0], "long slow run must be fetched from cache"
    assert 502 not in series, "short run must not be selected by duration filter"
    assert 503 not in series, "structured-interval workout must be excluded"


def test_kpi_keys_include_aerobic_decoupling():
    assert "load.decoupling_mean" in [k for k, _ in d.KPI_KEYS]


def test_session_view_carries_decoupling_dots():
    from profile import default_profile

    from session import build_session_view

    a = _long_run(504, date(2026, 5, 10))
    n = 104
    series = {
        504: (
            [145.0] * (n // 2) + [155.0] * (n - n // 2),
            [3.0] * n,
            [float(i * 60000) for i in range(n)],
        )
    }
    view = build_session_view(
        [a], default_profile(age=40, hrrest=60, sex="M"), None, None, None, series
    )
    assert "load.decoupling" in view.series
    assert view.context["load.decoupling"]["params"]["unit"] == "fraction"


class _FakeCol:
    def __init__(self, calls):
        self._calls = calls

    def metric(self, *args, **kwargs):
        self._calls.append(("metric", args))


class _FakeSt:
    """Minimal streamlit stub: records calls, returns falsy for widgets."""

    def __init__(self):
        self.calls = []

    def columns(self, n):
        return [_FakeCol(self.calls) for _ in range(n)]

    def __getattr__(self, name):
        def _rec(*args, **kwargs):
            self.calls.append((name, args))
            return None

        return _rec


def _decoupling_view():
    from profile import default_profile

    from session import build_session_view

    acts = [_long_run(600 + i, date(2026, 7, 6) + timedelta(days=i)) for i in range(6)]
    n = 104
    series = {
        a.activity_id: (
            [145.0] * (n // 2) + [155.0] * (n - n // 2),
            [3.0] * n,
            [float(k * 60000) for k in range(n)],
        )
        for a in acts
    }
    return build_session_view(
        acts, default_profile(age=40, hrrest=60, sex="M"), None, None, None, series
    )


def test_render_kpis_shows_decoupling_mean(monkeypatch):
    view = _decoupling_view()
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)
    d.render_kpis(windowed, view, "km", ["Aerobic decoupling"])
    metrics = [a for name, a in fake.calls if name == "metric"]
    assert metrics and metrics[0][1] == "6.9%"


def test_render_load_tab_decoupling_chart(monkeypatch):
    """Decoupling lives in the Fitness tab now — the Load tab must not render it."""
    view = _decoupling_view()
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)
    d.render_load_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert not any("decoupling" in str(getattr(f.layout.title, "text", "")) for f in figs)
    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert not any("route-matched" in c for c in captions)


def test_render_fitness_tab_decoupling_chart(monkeypatch):
    view = _decoupling_view()
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)
    d.render_fitness_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert any("decoupling" in str(getattr(f.layout.title, "text", "")) for f in figs)
    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert any("route-matched" in c for c in captions)
