"""Tests for dashboard refresh window logic.

Root cause of the recurring timeout: `refresh_garmin` re-paginated the entire
365-day history every run (~85 100-row pages, >90s) instead of only new pages.
These tests pin the incremental-fetch behavior: subsequent refreshes narrow the
Garmin window to activities newer than the latest already in the local store,
while the first refresh (empty store) keeps the full window.
"""

import json
import math
import time
from contextlib import nullcontext
from datetime import date, timedelta
from profile import RunnerProfile
from unittest import mock

import pytest

import chart_theme
import dashboard as d
from dashboard import (
    DEFAULT_WINDOW_DAYS,
    FETCH_DAYS,
    compute_fetch_start,
    period_bounds,
    race_time_ticks,
)
from normalize import Activity


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


def test_refresh_garmin_narrows_window_when_store_has_activities(monkeypatch, tmp_path):
    """Subsequent refreshes must only ask Garmin for activities newer than what
    is already persisted locally — this is the regression guard for the volume-
    driven timeout."""
    end = date(2026, 8, 31)
    latest = date(2026, 8, 28)
    expected_start = date(2026, 8, 29)

    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = latest
    fake_store.load_activities.return_value = []  # catch-up pass (b) finds nothing
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.cache_dir = tmp_path  # real dir: RPE/weather backfill cache-first check
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


def test_refresh_garmin_full_window_when_store_empty(monkeypatch, tmp_path):
    """First refresh: empty local store => fetch full FETCH_DAYS window."""
    end = date(2026, 8, 31)
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=end)))

    fake_store = mock.Mock()
    fake_store.latest_activity_date.return_value = None
    fake_store.load_activities.return_value = []  # catch-up pass (b) finds nothing
    monkeypatch.setattr("dashboard.get_store", lambda: fake_store)

    fake_gw = mock.Mock()
    fake_gw.cache_dir = tmp_path  # real dir: RPE/weather backfill cache-first check
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
    fake_store.load_activities.return_value = []  # catch-up pass (b) finds nothing
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


# --- RPE/Feel/weather backfill in refresh (cache-first, timeout-bounded) ---

_SUMMARY_OK = {"summaryDTO": {"directWorkoutRpe": 75, "directWorkoutFeel": 100}}
_WEATHER_OK = {
    "temp": 46.0,
    "apparentTemp": 44.0,
    "relativeHumidity": 55.0,
    "windSpeed": 6.0,
    "windDirectionCompassPoint": "NNE",
    "weatherTypeDTO": {"desc": "Mostly Cloudy"},
}


def _raw(aid: int) -> dict:
    """Minimal Garmin activity-list payload for from_summary."""
    return {
        "activityId": aid,
        "activityName": "Fake Run",
        "startTimeLocal": "2026-09-03 10:00:00",
        "activityType": {"typeKey": "running"},
        "distance": 5000.0,
        "duration": 1800.0,
        "elapsedDuration": 1800.0,
        "averageHR": 150.0,
        "maxHR": 165.0,
    }


def _refresh_backfill_setup(monkeypatch, tmp_path, raws):
    """Wire refresh_garmin to a fake gateway + a real store under tmp_path.

    Returns (gw, store): tests attach summary/weather stubs (or pre-seed the
    cache) to gw and assert persistence against store.
    """
    from store import MetricStore

    gw = mock.Mock()
    gw.cache_dir = tmp_path  # real dir: the backfill's cache-first check hits it
    gw.fetch_activities.return_value = raws
    gw.fetch_lactate_threshold.return_value = {}
    gw.fetch_race_predictions_trend.return_value = []
    gw.fetch_vo2max_trend.return_value = []
    store = MetricStore(str(tmp_path / "refresh.sqlite"))
    monkeypatch.setattr("dashboard.date", mock.Mock(today=mock.Mock(return_value=date(2026, 9, 3))))
    monkeypatch.setattr("dashboard.GarminGateway", lambda **kw: gw)
    monkeypatch.setattr("dashboard.get_store", lambda: store)
    return gw, store


def test_refresh_garmin_backfills_rpe_weather_before_save(monkeypatch, tmp_path):
    """Fresh id → summary+weather fetched, parsed, merged onto the Activity
    objects before they hit the store: main() saves the list refresh_garmin
    returns (INSERT OR REPLACE), so the merge has to happen inside the refresh
    for the columns to persist."""
    gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [_raw(5001)])
    gw.fetch_activity_summary.return_value = _SUMMARY_OK
    gw.fetch_activity_weather.return_value = _WEATHER_OK

    acts, _lt, _race, _vo2 = d.refresh_garmin()

    gw.fetch_activity_summary.assert_called_once_with(activity_id=5001)
    gw.fetch_activity_weather.assert_called_once_with(activity_id=5001)
    assert acts[0].rpe_10 == 7.5
    assert acts[0].feel == "Very Strong"
    assert acts[0].wx_temp == 46.0
    assert acts[0].wx_desc == "Mostly Cloudy"

    # What main() does next: one save must carry the backfilled columns.
    store.save_activities(acts)
    row = {a.activity_id: a for a in store.load_activities()}[5001]
    store.close()
    assert row.rpe_10 == 7.5
    assert row.feel == "Very Strong"
    assert row.wx_temp == 46.0
    assert row.wx_feels == 44.0
    assert row.wx_humidity == 55.0
    assert row.wx_wind == 6.0
    assert row.wx_wind_dir == "NNE"
    assert row.wx_desc == "Mostly Cloudy"
    assert row.wx_unit == "F"


def test_refresh_garmin_backfill_cache_hit_skips_fetch(monkeypatch, tmp_path):
    """Cache-first: an id with summary/weather already on disk merges from the
    cache — the network stubs must never be called (cheap repeat refreshes)."""
    (tmp_path / "activity_summary_5002.json").write_text(
        json.dumps({"summaryDTO": {"directWorkoutRpe": 40, "directWorkoutFeel": 25}})
    )
    (tmp_path / "activity_weather_5002.json").write_text(
        json.dumps({"temp": 50.0, "weatherTypeDTO": {"desc": "Fair"}})
    )
    gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [_raw(5002)])

    acts, *_ = d.refresh_garmin()

    gw.fetch_activity_summary.assert_not_called()
    gw.fetch_activity_weather.assert_not_called()
    assert acts[0].rpe_10 == 4.0
    assert acts[0].feel == "Weak"
    assert acts[0].wx_temp == 50.0
    assert acts[0].wx_desc == "Fair"
    store.close()


def test_refresh_garmin_backfill_skips_failed_activity(monkeypatch, tmp_path):
    """Skip-on-failure: fetches raising for one id must not block the refresh —
    the remaining activities still backfill and persist; the failed one stays
    NULL and is re-selected by the next refresh's store-driven catch-up pass
    (`_backfill_catchup`) once its fetch succeeds."""
    gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [_raw(5001), _raw(5002)])

    def _summary(activity_id):
        if activity_id == 5002:
            raise RuntimeError("summary fetch boom")
        return _SUMMARY_OK

    def _weather(activity_id):
        if activity_id == 5002:
            raise TimeoutError("weather stalled")
        return _WEATHER_OK

    gw.fetch_activity_summary.side_effect = _summary
    gw.fetch_activity_weather.side_effect = _weather

    acts, *_ = d.refresh_garmin()  # must complete, not raise

    by_id = {a.activity_id: a for a in acts}
    assert len(acts) == 2
    assert by_id[5001].rpe_10 == 7.5
    assert by_id[5001].wx_temp == 46.0
    assert by_id[5002].rpe_10 is None
    assert by_id[5002].feel is None
    assert by_id[5002].wx_temp is None
    assert by_id[5002].wx_desc is None

    # "saves the rest": the successful activity's values reach the store.
    store.save_activities(acts)
    rows = {a.activity_id: a for a in store.load_activities()}
    store.close()
    assert rows[5001].rpe_10 == 7.5
    assert rows[5001].wx_desc == "Mostly Cloudy"
    assert rows[5002].rpe_10 is None
    assert rows[5002].wx_temp is None


def test_backfill_stops_when_time_budget_exhausted(tmp_path):
    """Budget guard: past the deadline the pass stops without fetching, so a
    first-run backfill of hundreds of ids cannot overrun the 90s refresh guard.
    Rows already persisted keep their values; the untouched (NULL) rows resume
    via the next refresh's catch-up pass — see
    test_refresh_garmin_catchup_backfills_stored_null_rows."""
    from normalize import from_summary

    gw = mock.Mock()
    gw.cache_dir = tmp_path
    acts = [from_summary(_raw(5001)), from_summary(_raw(5002))]

    d._backfill_rpe_weather(gw, acts, deadline=time.monotonic() - 1)

    gw.fetch_activity_summary.assert_not_called()
    gw.fetch_activity_weather.assert_not_called()
    assert all(a.rpe_10 is None and a.wx_temp is None for a in acts)


def test_refresh_garmin_bounds_backfill_deadline_inside_fetch_timeout(monkeypatch, tmp_path):
    """The deadline handed to the backfill sits FETCH_TIMEOUT - BACKFILL_RESERVE_S
    after the refresh started — backfill cannot consume the whole 90s guard."""
    captured = {}

    def _spy(gw, acts, deadline):
        captured["deadline"] = deadline
        captured["now"] = time.monotonic()

    monkeypatch.setattr(d, "_backfill_rpe_weather", _spy)
    _gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [_raw(5001)])

    d.refresh_garmin()

    remaining = captured["deadline"] - captured["now"]
    assert 0 < remaining <= d.FETCH_TIMEOUT - d.BACKFILL_RESERVE_S + 1
    store.close()


def test_refresh_garmin_catchup_backfills_stored_null_rows(monkeypatch, tmp_path):
    """Ruling 7 recovery: a stored row still holding NULL backfill columns is
    fetched by the store-driven catch-up pass on a later refresh even though it
    was NOT in the fresh fetch (incremental windows never re-request it)."""
    from normalize import from_summary

    # Nothing new from Garmin (incremental no-op) — (b) must still run.
    gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [])
    # Row saved earlier with NULL columns (e.g. budget-stopped on a prior run).
    store.save_activities([from_summary(_raw(6001))])
    gw.fetch_activity_summary.return_value = _SUMMARY_OK
    gw.fetch_activity_weather.return_value = _WEATHER_OK

    acts, *_ = d.refresh_garmin()

    assert acts == []
    gw.fetch_activity_summary.assert_called_once_with(activity_id=6001)
    gw.fetch_activity_weather.assert_called_once_with(activity_id=6001)
    # The catch-up pass persists its own merges — acts is empty, so no
    # caller-level save happens on this path.
    row = {a.activity_id: a for a in store.load_activities()}[6001]
    store.close()
    assert row.rpe_10 == 7.5
    assert row.feel == "Very Strong"
    assert row.wx_temp == 46.0
    assert row.wx_desc == "Mostly Cloudy"


def test_backfill_budget_exhausted_resumes_on_later_refresh(monkeypatch, tmp_path):
    """When pass (a) exhausts the budget, pass (b) does nothing too — but the
    NULL rows are persisted, and a later refresh resumes them (freshness first,
    catch-up after): NULL rows shrink over successive refreshes."""
    from normalize import from_summary

    gw, store = _refresh_backfill_setup(monkeypatch, tmp_path, [_raw(7001)])
    store.save_activities([from_summary(_raw(7000))])  # older stored row, all NULL
    gw.fetch_activity_summary.return_value = _SUMMARY_OK
    gw.fetch_activity_weather.return_value = _WEATHER_OK

    # Refresh 1: deadline == refresh start → no budget for either pass.
    monkeypatch.setattr(d, "BACKFILL_RESERVE_S", d.FETCH_TIMEOUT)
    acts1, *_ = d.refresh_garmin()
    assert len(acts1) == 1  # fresh activity fetched, backfill skipped
    gw.fetch_activity_summary.assert_not_called()  # (a) skipped AND (b) skipped
    gw.fetch_activity_weather.assert_not_called()
    store.save_activities(acts1)  # main() persists the fresh list (still NULLs)
    rows = {a.activity_id: a for a in store.load_activities()}
    assert rows[7000].rpe_10 is None and rows[7001].rpe_10 is None

    # Refresh 2: budget back → (a) covers the fresh id first, then (b) catches
    # up the stored NULL row.
    monkeypatch.setattr(d, "BACKFILL_RESERVE_S", 15)
    acts2, *_ = d.refresh_garmin()
    store.save_activities(acts2)  # main() save
    ids = [c.kwargs["activity_id"] for c in gw.fetch_activity_summary.call_args_list]
    assert ids[0] == 7001  # freshness wins: pass (a) ran before the catch-up
    assert 7000 in ids  # catch-up resumed the stored NULL row
    rows = {a.activity_id: a for a in store.load_activities()}
    store.close()
    assert rows[7000].rpe_10 == 7.5
    assert rows[7000].wx_temp == 46.0
    assert rows[7001].rpe_10 == 7.5  # NULL rows shrank to zero
    assert rows[7001].wx_desc == "Mostly Cloudy"


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
        Activity(
            activity_id=1,
            sport="running",
            date=_date(2026, 8, 10),
            ts_ms=0,
            distance_m=5000,
            duration_s=1800,
            elapsed_s=1800,
            zone_s={},
            ele_gain_m=50,
            vo2max=None,
            avg_hr=150,
            max_hr=170,
            aerobic_te=None,
            anaerobic_te=None,
            avg_speed=2.78,
            fastest_split_1609=None,
            name="Run - RF24 (Foundation)",
            has_intervals=False,
        ),
        Activity(
            activity_id=2,
            sport="running",
            date=_date(2026, 8, 17),
            ts_ms=0,
            distance_m=5000,
            duration_s=1750,
            elapsed_s=1750,
            zone_s={},
            ele_gain_m=40,
            vo2max=None,
            avg_hr=152,
            max_hr=172,
            aerobic_te=None,
            anaerobic_te=None,
            avg_speed=2.86,
            fastest_split_1609=None,
            name="Run - RF24 (Foundation)",
            has_intervals=False,
        ),
        Activity(
            activity_id=3,
            sport="running",
            date=_date(2026, 8, 12),
            ts_ms=0,
            distance_m=8000,
            duration_s=3000,
            elapsed_s=3000,
            zone_s={},
            ele_gain_m=30,
            vo2max=None,
            avg_hr=145,
            max_hr=165,
            aerobic_te=None,
            anaerobic_te=None,
            avg_speed=2.67,
            fastest_split_1609=None,
            name="Easy run - no code",
            has_intervals=False,
        ),
        Activity(
            activity_id=4,
            sport="treadmill",
            date=_date(2026, 8, 14),
            ts_ms=0,
            distance_m=5000,
            duration_s=1900,
            elapsed_s=1900,
            zone_s={},
            ele_gain_m=0,
            vo2max=None,
            avg_hr=148,
            max_hr=168,
            aerobic_te=None,
            anaerobic_te=None,
            avg_speed=2.63,
            fastest_split_1609=None,
            name="Treadmill - RF24 (Foundation)",
            has_intervals=False,
        ),
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

    def caption(self, *args, **kwargs):
        self._calls.append(("caption", args))


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


def _patch_st(monkeypatch, fake):
    """Route both the dashboard module and the shared chart theme to one fake."""
    for module in (d, chart_theme):
        monkeypatch.setattr(module, "st", fake)


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
    _patch_st(monkeypatch, fake)
    d.render_kpis(windowed, view, "km", ["Aerobic decoupling"])
    metrics = [a for name, a in fake.calls if name == "metric"]
    assert metrics and metrics[0][1] == "6.9%"


def test_render_load_tab_decoupling_chart(monkeypatch):
    """Decoupling lives in the Fitness tab now — the Load tab must not render it."""
    view = _decoupling_view()
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_load_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert not any("decoupling" in str(getattr(f.layout.title, "text", "")) for f in figs)
    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert not any("route-matched" in c for c in captions)


def test_render_fitness_tab_decoupling_chart(monkeypatch):
    view = _decoupling_view()
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert any("decoupling" in str(getattr(f.layout.title, "text", "")) for f in figs)
    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert any("route-matched" in c for c in captions)


def test_render_kpis_lt_hr_falls_back_to_best20(monkeypatch):
    """No Garmin LT record: the LT HR tile shows the 20-min best-effort
    anchor, clearly labeled, instead of a dash."""
    view = _decoupling_view()
    assert "load.lt_hr" not in view.series
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_kpis(windowed, view, "km", ["LT HR"])
    metrics = [a for name, a in fake.calls if name == "metric"]
    assert metrics and metrics[0][0] == "LT HR (best-effort)" and metrics[0][1] == "138 bpm"


def test_render_kpis_lt_hr_prefers_garmin(monkeypatch):
    """A measured Garmin LT record always wins over the best-effort anchor."""
    from profile import default_profile

    from session import build_session_view

    acts = [_long_run(700 + i, date(2026, 7, 6) + timedelta(days=i)) for i in range(2)]
    lt = {"speed_and_heart_rate": {"heartRate": 158, "speed": 3.5, "calendarDate": "2026-07-01"}}
    view = build_session_view(
        acts, default_profile(age=40, hrrest=60, sex="M"), lt, None, None, None
    )
    assert "load.lt_hr" in view.series
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_kpis(windowed, view, "km", ["LT HR"])
    metrics = [a for name, a in fake.calls if name == "metric"]
    assert metrics and metrics[0][0] == "LT HR" and metrics[0][1] == "158 bpm"


# --- Sub-project A views: age-graded trend, rollups, best-mile (sub-project A) ---

PROFILE_40M = RunnerProfile(hrmax=180, hrrest=60, sex="M", birth_year=1986, units="metric")


def _graded_view():
    from session import build_session_view

    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
        Activity(2, "running", date(2026, 1, 17), 0, 10000.0, 2000.0, 2000.0, 150.0, 170.0),
    ]
    return build_session_view(acts, PROFILE_40M)


def test_render_fitness_tab_age_graded_chart(monkeypatch):
    view = _graded_view()
    assert "age_graded.pct" in view.series
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert any("Age-graded" in str(getattr(f.layout.title, "text", "")) for f in figs)


def test_volume_tab_sidebar_agg_toggle_weekly_default(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
    ]
    d.render_volume_tab(acts, view, windowed, "km", "weekly")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert figs and "Weekly" in str(figs[0].layout.title.text)


def test_volume_tab_sidebar_agg_toggle_monthly(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    fake.radio = lambda *a, **k: "Monthly"
    _patch_st(monkeypatch, fake)
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
    ]
    d.render_volume_tab(acts, view, windowed, "km", "monthly")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert figs and "Monthly" in str(figs[0].layout.title.text)


def test_volume_tab_captions_partial_buckets(monkeypatch):
    """The volume tab must name the buckets that spill past the window."""
    view = _graded_view()
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
    ]
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    since, until = date(2026, 1, 1), date(2026, 1, 7)
    windowed = view.windowed(since, until)
    d.render_volume_tab(acts, view, windowed, "km", "weekly", since=since, until=until)
    caps = " ".join(str(a[0]) for name, a in fake.calls if name == "caption")
    assert "Partial buckets" in caps
    assert "2026-01-05" in caps  # the Mon–Sun week that runs past 2026-01-07


def test_volume_tab_no_rollup_pane(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    # Ensure radio is not called at all
    fake.radio = lambda *a, **k: None  # return falsy so widget value is ignored
    _patch_st(monkeypatch, fake)
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
    ]
    d.render_volume_tab(acts, view, windowed, "km", "weekly")
    # radio should not have been called (or if called, returned None)
    radio_calls = [a for name, a in fake.calls if name == "radio"]
    assert not radio_calls, f"radio should not be called, but was called with {radio_calls}"
    # no dataframe call should have a "period" key
    frames = [a[0] for name, a in fake.calls if name == "dataframe"]
    assert not any("period" in str(f) for f in frames)


def test_volume_tab_duration_rolling_trace_is_smoothed(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
        Activity(2, "running", date(2026, 1, 17), 0, 10000.0, 2000.0, 2000.0, 150.0, 170.0),
    ]
    d.render_volume_tab(acts, view, windowed, "km", "weekly")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    dur_figs = [f for f in figs if "duration (hours)" in str(getattr(f.layout.title, "text", ""))]
    assert dur_figs, "no duration chart rendered"
    fig = dur_figs[0]
    traces = {t.name: list(t.y) for t in fig.data}
    assert "rolling 4wk" in traces, "rolling 4wk trace missing"
    raw = traces["running"]
    roll = traces["rolling 4wk"]
    assert len(raw) >= 2, "need multiple weeks to show smoothing"
    assert roll != raw, "rolling 4wk trace equals raw duration bars — fix: use volume.rolling_4wk()"


def test_render_activities_best_mile_and_graded(monkeypatch):
    acts = [
        Activity(
            1,
            "running",
            date(2026, 1, 10),
            0,
            5000.0,
            1000.0,
            1000.0,
            150.0,
            170.0,
            fastest_split_1609=320.0,
        ),
    ]
    fake = _FakeSt()
    recorded = []

    def _df(*a, **k):
        recorded.append(a)
        return mock.Mock(selection=mock.Mock(rows=[]))

    fake.dataframe = _df
    _patch_st(monkeypatch, fake)
    d.render_activities(acts, date(2026, 1, 1), date(2026, 1, 31), "km", PROFILE_40M)
    metrics = [a for name, a in fake.calls if name == "metric"]
    assert any("Best mile" in str(m[0]) for m in metrics)
    assert recorded and "graded" in recorded[0][0].columns


# --- Fastest-30s on demand (repetition comparison) ---


def _details_payload(speed=4.0, n=60, step_ms=1000.0, hr=170.0):
    """Live-shape details payload: n samples at a fixed speed, uniform step."""
    return {
        "metricDescriptors": [
            {"key": "directHeartRate", "metricsIndex": 0},
            {"key": "directSpeed", "metricsIndex": 1},
            {"key": "directTimestamp", "metricsIndex": 2},
        ],
        "activityDetailMetrics": [{"metrics": [hr, speed, i * step_ms]} for i in range(n)],
    }


def _coded_act(activity_id, name, day, sport="running"):
    return Activity(
        activity_id=activity_id,
        sport=sport,
        date=date(2026, 8, day),
        ts_ms=0,
        distance_m=8000.0,
        duration_s=2400.0,
        elapsed_s=2500.0,
        avg_hr=150.0,
        max_hr=170.0,
        zone_s={},
        ele_gain_m=15.0,
        avg_speed=3.3,
        name=name,
    )


def _rep_gateway(fetched, fail_ids=()):
    """Gateway double: records fetch attempts, raises for fail_ids."""

    class _Gw:
        def __init__(self, cache_dir=None):
            self.cache_dir = cache_dir

        def fetch_activity_details(self, activity_id):
            fetched.append(activity_id)
            if activity_id in fail_ids:
                raise RuntimeError("dead details endpoint")
            return _details_payload()

    return _Gw


def _record_pace(monkeypatch):
    """Capture (decimal_minutes, units) calls of the row-data pace formatter."""
    calls = []
    monkeypatch.setattr(d, "_fmt_pace_min", lambda v, u: calls.append((v, u)) or f"{v:.3f}")
    return calls


def test_render_repetitions_fetches_missing_group_details(monkeypatch, tmp_path):
    """Selecting a coded workout fetches details only for that group's reps
    that lack a disk cache — never for other activities, never from the
    network for an id already cached."""
    acts = [
        _coded_act(1, "Winnetka - RF24 (Foundation Run)", 10),
        _coded_act(2, "Lincoln - RF24 (Foundation Run)", 15),
        _coded_act(3, "Lincoln - RHR22 (Hill Repetitions Run)", 20),  # other code
    ]
    _cached_details(tmp_path, 1, minutes=60)  # rep 1 already on disk

    fetched = []
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _rep_gateway(fetched))
    pace_calls = _record_pace(monkeypatch)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_repetitions(acts, 2, "km")

    assert fetched == [2]  # id 1 served from disk cache, id 3 is another code
    # both reps produced a pace in row data: disk-cached 3.0 m/s, fetched 4.0 m/s
    assert len(pace_calls) == 2
    (v1, u1), (v2, u2) = pace_calls
    assert (v1, u1) == (pytest.approx(1000.0 / 3.0 / 60.0), "km")
    assert (v2, u2) == (pytest.approx(1000.0 / 4.0 / 60.0), "km")
    # Task 5 flip: the Fast30s column is visible (strip removed), in reps'
    # chronological order, formatted by _fmt_pace_min (stubbed above)
    shown = [a for name, a in fake.calls if name == "dataframe"][0][0]
    assert shown and all("fast30s" in row for row in shown)
    assert [row["fast30s"] for row in shown] == ["5.556", "4.167"]
    # plus the shared RPE/Feel/weather columns — all unbackfilled → '—'
    for row in shown:
        assert all(row[k] == "—" for k in ("RPE", "Feel", "Temp", "Hum", "Wind", "Wx"))


def test_render_repetitions_failed_fetch_does_not_break_group(monkeypatch, tmp_path):
    """One dead details fetch leaves that rep's value None; the rest of the
    group still fetches and computes (never raises)."""
    acts = [
        _coded_act(11, "Winnetka - RF24 (Foundation Run)", 10),
        _coded_act(12, "Lincoln - RF24 (Foundation Run)", 15),
    ]
    fetched = []
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _rep_gateway(fetched, fail_ids={11}))
    pace_calls = _record_pace(monkeypatch)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_repetitions(acts, 12, "km")  # must not raise

    assert sorted(fetched) == [11, 12]  # both attempted; the dead one didn't abort the rest
    assert len(pace_calls) == 1  # only the healthy rep produced a pace value
    assert pace_calls[0] == (pytest.approx(1000.0 / 4.0 / 60.0), "km")
    # the dead rep degrades to '—' in the visible Fast30s column (oldest first)
    shown = [a for name, a in fake.calls if name == "dataframe"][0][0]
    assert [row["fast30s"] for row in shown] == ["—", "4.167"]


def test_render_repetitions_gate_includes_treadmill_excludes_strength(monkeypatch, tmp_path):
    """Ruling 8: treadmill pace is real — it passes the fetch gate alongside
    outdoor running. Strength/cross reps have no meaningful pace: never
    fetched, value '—'."""
    acts = [
        _coded_act(21, "Winnetka - RF24 (Foundation Run)", 10, sport="running"),
        _coded_act(22, "Winnetka - RF24 (Foundation Run)", 15, sport="strength"),
        _coded_act(23, "Winnetka - RF24 (Foundation Run)", 20, sport="treadmill"),
    ]
    fetched = []
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _rep_gateway(fetched))
    pace_calls = _record_pace(monkeypatch)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_repetitions(acts, 21, "km")

    assert fetched == [21, 23]  # the strength rep's details are never requested
    assert len(pace_calls) == 2  # running + treadmill both got a pace value
    shown = [a for name, a in fake.calls if name == "dataframe"][0][0]
    # both fetched reps parse to 4.0 m/s → 1000/4/60 = 4.167; strength → '—'
    assert [row["fast30s"] for row in shown] == ["4.167", "—", "4.167"]


def test_render_activities_no_selection_never_fetches_details(monkeypatch):
    """Without a selected row there is no comparison group → no gateway, no fetch."""
    acts = [
        _coded_act(31, "Winnetka - RF24 (Foundation Run)", 10),
        _coded_act(32, "Lincoln - RF24 (Foundation Run)", 15),
    ]
    gw_factory = mock.Mock(side_effect=AssertionError("gateway built without a selection"))
    monkeypatch.setattr(d, "GarminGateway", gw_factory)
    fake = _FakeSt()
    fake.dataframe = lambda *a, **k: mock.Mock(selection=mock.Mock(rows=[]))
    _patch_st(monkeypatch, fake)

    d.render_activities(acts, date(2026, 8, 1), date(2026, 8, 31), "km", PROFILE_40M)

    gw_factory.assert_not_called()


# --- RPE / Feel / weather columns + Fast30s chart trace (Task 5) ---


def _wx_act(activity_id, day, **wx):
    """Outdoor run carrying the persisted RPE/Feel/weather fields."""
    fields = {
        "rpe_10": 6.0,
        "feel": "Normal",
        "wx_temp": 46.0,
        "wx_feels": 44.0,
        "wx_humidity": 65.0,
        "wx_wind": 8.0,
        "wx_wind_dir": "nw",
        "wx_desc": "Fair",
        "wx_unit": "F",
    }
    fields.update(wx)
    return Activity(
        activity_id,
        "running",
        date(2026, 1, day),
        0,
        5000.0,
        1000.0,
        1000.0,
        150.0,
        170.0,
        **fields,
    )


def _render_activities_rows(monkeypatch, acts, units):
    """Render the flat list and return its rows keyed by date."""
    recorded = []
    fake = _FakeSt()

    def _df(*a, **k):
        recorded.append(a)
        return mock.Mock(selection=mock.Mock(rows=[]))

    fake.dataframe = _df
    _patch_st(monkeypatch, fake)
    d.render_activities(acts, date(2026, 1, 1), date(2026, 1, 31), units, PROFILE_40M)
    df = recorded[0][0]
    return df, {row["date"]: row for row in df.to_dict("records")}


def test_render_activities_rpe_feel_weather_rows_metric(monkeypatch):
    """Flat list shows RPE/Feel/Temp/Hum/Wind/Wx — '—' when a field is missing
    (pre-backfill rows), °C/km/h display when Units = km, and never a Fast30s
    column (the owner-rejected precompute stays comparison-only)."""
    acts = [
        _wx_act(1, 10),  # full backfill
        _wx_act(
            2,
            17,
            rpe_10=7.5,
            feel="60?",  # out-of-range Feel keeps the parse's '?' suffix, as-is
            wx_temp=41.0,
            wx_humidity=None,
            wx_wind=None,
            wx_wind_dir=None,
            wx_desc=None,
        ),  # partial
        _wx_act(
            3,
            24,
            rpe_10=None,
            feel=None,
            wx_temp=None,
            wx_feels=None,
            wx_humidity=None,
            wx_wind=None,
            wx_wind_dir=None,
            wx_desc=None,
            wx_unit=None,
        ),  # never backfilled
    ]
    df, rows = _render_activities_rows(monkeypatch, acts, "km")

    assert "Fast30s" not in df.columns and "fast30s" not in df.columns
    for col in ("RPE", "Feel", "Temp", "Hum", "Wind", "Wx"):
        assert col in df.columns

    full = rows["2026-01-10"]
    assert full["RPE"] == "6/10"
    assert full["Feel"] == "Normal"
    assert full["Temp"] == "8°C"  # 46°F converted for display only
    assert full["Hum"] == "65%"
    assert full["Wind"] == "13 km/h nw"  # 8 mph → km/h for metric display
    assert full["Wx"] == "Fair"

    partial = rows["2026-01-17"]
    assert partial["RPE"] == "7.5/10"  # one decimal kept for half RPEs
    assert partial["Feel"] == "60?"  # raw label (with '?') rendered unchanged
    assert partial["Temp"] == "5°C"  # 41°F → 5°C
    assert partial["Hum"] == "—"
    assert partial["Wind"] == "—"
    assert partial["Wx"] == "—"

    bare = rows["2026-01-24"]
    assert all(bare[col] == "—" for col in ("RPE", "Feel", "Temp", "Hum", "Wind", "Wx"))


def test_render_activities_weather_rows_imperial_keeps_stored_units(monkeypatch):
    """Miles display shows the stored °F/mph raw values — no conversion, no
    mutation of what's on the Activity."""
    act = _wx_act(1, 10)
    df, rows = _render_activities_rows(monkeypatch, [act], "miles")

    assert "Fast30s" not in df.columns
    row = rows["2026-01-10"]
    assert row["Temp"] == "46°F"
    assert row["Wind"] == "8 mph nw"
    assert row["Hum"] == "65%"
    assert row["RPE"] == "6/10"
    # display formatting never writes back into the model
    assert act.wx_temp == 46.0 and act.wx_wind == 8.0 and act.rpe_10 == 6.0


def test_render_repetitions_chart_adds_dashed_fast30s_trace(monkeypatch, tmp_path):
    """The performance-over-repetitions chart gains a Fast30s pace trace as
    the third pace series (between GAP and avg HR), dashed; reps without pace
    data plot a gap instead of a value."""
    acts = [
        _coded_act(41, "Winnetka - RF24 (Foundation Run)", 10, sport="running"),
        _coded_act(42, "Winnetka - RF24 (Foundation Run)", 15, sport="strength"),
        _coded_act(43, "Winnetka - RF24 (Foundation Run)", 20, sport="treadmill"),
    ]
    _cached_details(tmp_path, 41)  # disk cache: speed 3.0 m/s
    fetched = []
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _rep_gateway(fetched))  # payload: 4.0 m/s
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_repetitions(acts, 41, "km")

    assert fetched == [43]  # 41 cached, 42 strength never fetched
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    fig = next(
        f
        for f in figs
        if "Performance over repetitions" in str(getattr(f.layout.title, "text", ""))
    )
    names = [t.name for t in fig.data]
    assert names == [
        "avg pace (min/unit)",
        "GAP (min/unit)",
        "Fast30s (min/unit)",
        "avg HR (bpm)",
    ]
    fast = fig.data[names.index("Fast30s (min/unit)")]
    assert fast.line.dash == "dash"
    assert list(fast.y) == [
        pytest.approx(1000.0 / 3.0 / 60.0),
        None,  # strength rep: no pace data → gap
        pytest.approx(1000.0 / 4.0 / 60.0),
    ]


# --- Task 9: Runalyze VDOT surfaces (shape overlay, prognosis, paces) ---

RUNALYZE_PROFILE = RunnerProfile(hrmax=190, hrrest=55, sex="M", birth_year=1986, units="metric")
RUNALYZE_SINCE = date(2026, 6, 1)
RUNALYZE_UNTIL = date(2026, 7, 5)


def _runalyze_run(activity_id, day, distance_m, duration_s, hr, event_type=None):
    return Activity(
        activity_id,
        "running",
        day,
        0,
        float(distance_m),
        float(duration_s),
        float(duration_s),
        float(hr),
        float(hr) + 10,
        event_type=event_type,
    )


def _runalyze_acts():
    """Synthetic qualifying runs plus one race-tagged run.

    The race tag anchors the corrector (shape × best-3 ratio).
    """
    acts = [
        _runalyze_run(
            100 + i, date(2026, 6, 1) + timedelta(days=i * 2), 9000 + i * 100, 2700 + i * 20, 150
        )
        for i in range(14)
    ]
    acts.append(_runalyze_run(200, date(2026, 6, 28), 21097.5, 5400, 168, event_type="race"))
    acts.append(_runalyze_run(201, date(2026, 7, 2), 30000, 9000, 148))
    acts.append(_runalyze_run(202, date(2026, 7, 5), 12000, 4000, 140))
    return acts


def _runalyze_view():
    """`_runalyze_acts()` with the Garmin VO2max payload the overlay pairs with."""
    from session import build_session_view

    vo2max = [
        {"generic": {"calendarDate": "2026-06-15", "vo2MaxPreciseValue": 48.5}},
        {"generic": {"calendarDate": "2026-07-05", "vo2MaxPreciseValue": 47.0}},
    ]
    return build_session_view(_runalyze_acts(), RUNALYZE_PROFILE, None, None, vo2max)


def _runalyze_shape_only_view():
    """Same runs, no Garmin payload: `vdot.shape` exists, `fitness.vo2max` does not."""
    from session import build_session_view

    return build_session_view(_runalyze_acts(), RUNALYZE_PROFILE, None, None, None)


def _runalyze_windowed(view):
    return view.windowed(RUNALYZE_SINCE, RUNALYZE_UNTIL)


def _captured(fake, name):
    return [a[0] for n, a in fake.calls if n == name]


def _one_row(value, day="2026-07-05"):
    """One-row daily series, the shape `windowed` hands the surface helpers."""
    import pandas as pd

    return pd.Series([value], index=pd.DatetimeIndex([pd.Timestamp(day)]))


def _prognosis_series(values):
    """Windowed-like mapping of the seven `prognosis.<dist>` keys."""
    return {
        f"prognosis.{km:g}": _one_row(value)
        for km, value in zip((1.0, 3.0, 5.0, 10.0, 21.1, 42.2, 50.0), values, strict=True)
    }


def _prognosis_values():
    return (204.375, 681.5, 1177.15, 2440.43, 5405.23, 11265.42, 13429.69)


def test_prognosis_rows_columns_and_absence_rules():
    """Raw always present; Damped only where damped rows exist; the block's
    hide rule is the missing `prognosis.5` row, never a filler cell."""
    from dashboard import prognosis_rows

    windowed = _prognosis_series(_prognosis_values())
    rows = prognosis_rows(windowed)
    assert [r["Distance"] for r in rows] == [
        "1 km",
        "3 km",
        "5 km",
        "10 km",
        "21.1 km",
        "42.2 km",
        "50 km",
    ]
    assert all(set(r) == {"Distance", "Raw"} for r in rows)  # no damped rows → no column
    assert [r["Raw"] for r in rows] == [
        "3:24",
        "11:22",
        "19:37",
        "40:40",
        "1:30:05",
        "3:07:45",
        "3:43:50",
    ]

    # A damped series for one distance only → the column appears, and every
    # distance without one is '—' (never a filler time).
    windowed["prognosis.42.2_damped"] = _one_row(15266.18)
    rows = prognosis_rows(windowed)
    assert [r["Damped"] for r in rows] == ["—"] * 5 + ["4:14:26", "—"]

    del windowed["prognosis.5"]
    assert prognosis_rows(windowed) == []


def test_pace_table_rows_uses_rounded_shape_and_full_ladder():
    """Runalyze's tools page rounds the current shape before the pace lookup;
    the ladder is 65..110 % in 5 % steps."""
    from dashboard import pace_table_rows

    rows = pace_table_rows(50.93441, "km")
    assert [r["% vVO2max"] for r in rows] == [f"{p}%" for p in range(65, 111, 5)]
    assert rows[0]["Pace"] == "5:21 /km"  # 65 % of 51 → 321 s/km
    assert rows[7]["Pace"] == "3:46 /km"  # 100 % → 226 s/km (unrounded shape → 227)
    assert rows[-1]["Pace"] == "3:30 /km"  # 110 % → 210 s/km
    assert pace_table_rows(None, "km") == []


def test_pace_table_rows_converts_for_miles():
    from dashboard import pace_table_rows

    rows = pace_table_rows(51, "miles")
    assert rows[0]["Pace"] == "8:36 /mi"  # 321 s/km × 1.609344 = 516.6 s/mi


def test_render_fitness_tab_shape_overlay(monkeypatch):
    """Surface 1: `vdot.shape` rides the existing Garmin VO2max figure as a
    second (orange) scatter, with its caption above the intact Garmin one."""
    view = _runalyze_view()
    windowed = _runalyze_windowed(view)
    assert not windowed["fitness.vo2max"].empty and not windowed["vdot.shape"].empty
    # The race tag is live in this fixture: the shape carries the best-3 corrector.
    assert view.context["vdot.shape"]["flags"]["corrector"] != pytest.approx(1.0)

    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(view, windowed, "km")

    figs = _captured(fake, "plotly_chart")
    fig = next(f for f in figs if "Firstbeat" in str(getattr(f.layout.title, "text", "")))
    assert [t.name for t in fig.data] == ["VO2max", "Runalyze shape"]
    shape_trace = fig.data[1]
    assert list(shape_trace.y) == [pytest.approx(v) for v in windowed["vdot.shape"].values]
    assert shape_trace.line.color == "#F18F01"

    captions = _captured(fake, "caption")
    shape_caption = next(c for c in captions if c.startswith("Runalyze-style shape:"))
    assert "30-day duration-weighted mean of per-run pace+HR VO2max" in shape_caption
    assert "× race corrector (best-3). Independent of Garmin Firstbeat." in shape_caption
    garmin_caption = next(c for c in captions if "Daily Garmin Firstbeat VO2max estimate" in c)
    assert captions.index(shape_caption) < captions.index(garmin_caption)


def test_render_fitness_tab_prognosis_table_and_marathon_detail(monkeypatch):
    """Surface 2 + 3: the prognosis table (Distance | Raw | Damped) and the
    marathon-shape detail tiles sit under the race-predictions block."""
    view = _runalyze_view()
    windowed = _runalyze_windowed(view)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(view, windowed, "km")

    frames = [f for f in _captured(fake, "dataframe") if f and "Distance" in f[0]]
    assert len(frames) == 1
    table = frames[0]
    assert len(table) == 7
    assert all(list(row) == ["Distance", "Raw", "Damped"] for row in table)
    assert table[2] == {"Distance": "5 km", "Raw": "19:37", "Damped": "19:37"}
    assert table[5] == {"Distance": "42.2 km", "Raw": "3:07:45", "Damped": "4:14:26"}

    captions = _captured(fake, "caption")
    assert any(
        c.startswith(
            "Runalyze-style prognosis ( Daniels-Gilbert, bisection-inverted; "
            "damped = marathon-shape-normalized)."
        )
        for c in captions
    )
    assert any(
        c.startswith(
            "Races you tag in Garmin Connect (event type `Race`) anchor these — the tag must "
            "be present at the activity's first fetch here; later tags are not picked up."
        )
        for c in captions
    )

    metrics = [a for n, a in fake.calls if n == "metric"]
    assert ("Marathon shape", "23%") in metrics
    assert ("Target weekly volume", "86.59 km") in metrics
    assert ("Target long jog", "30.53 km") in metrics


def test_render_prognosis_block_hidden_without_5k(monkeypatch):
    """No `prognosis.5` row (the emission ruling's empty-window case) → the
    whole block vanishes, marathon detail included."""
    view = _runalyze_view()
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_prognosis_block({}, view, "km")
    assert not [c for c in fake.calls if c[0] in ("dataframe", "subheader", "caption", "columns")]


def test_render_paces_table_from_current_shape(monkeypatch):
    """Surface 4: paces at 65..110 % of the current (rounded) shape, derived
    in-session — ten rows, no stored metric."""
    view = _runalyze_view()
    windowed = _runalyze_windowed(view)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_paces_table(windowed, "km")

    frames = _captured(fake, "dataframe")
    assert len(frames) == 1 and len(frames[0]) == 10
    assert list(frames[0][0]) == ["% vVO2max", "Pace"]
    assert frames[0][0] == {"% vVO2max": "65%", "Pace": "5:21 /km"}
    assert frames[0][-1] == {"% vVO2max": "110%", "Pace": "3:30 /km"}


def test_render_paces_table_hidden_without_shape(monkeypatch):
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_paces_table({}, "km")
    assert not [c for c in fake.calls if c[0] in ("dataframe", "subheader", "caption")]


# --- Task 9 review fixes: shape-only hint, guarded pct lookup, call sites ---


def test_render_fitness_tab_shape_only_keeps_garmin_hint(monkeypatch):
    """Shape-only fallback must not swallow the Garmin refresh prompt.

    With `vdot.shape` present and `fitness.vo2max` absent the chart still
    renders *and* the hint survives. A revert to `if vo2 is None or vo2.empty`
    would emit the hint and no chart, failing the figure assertion below.
    """
    view = _runalyze_shape_only_view()
    windowed = _runalyze_windowed(view)
    vo2 = windowed.get("fitness.vo2max")
    assert vo2 is None or vo2.empty
    assert not windowed["vdot.shape"].empty

    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(view, windowed, "km")

    figs = _captured(fake, "plotly_chart")
    fig = next(
        f
        for f in figs
        if "Runalyze shape (no Garmin VO2max trend)" in str(getattr(f.layout.title, "text", ""))
    )
    assert [t.name for t in fig.data] == ["Runalyze shape"]

    writes = _captured(fake, "write")
    assert any("Use **Refresh from Garmin**" in w and "/maxmet/daily" in w for w in writes)
    assert any(c.startswith("Runalyze-style shape:") for c in _captured(fake, "caption"))


def test_render_fitness_tab_hint_when_neither_series_exists(monkeypatch):
    """Neither series → the hint alone, no empty chart."""
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_fitness_tab(_runalyze_view(), {}, "km")

    writes = _captured(fake, "write")
    assert d.GARMIN_VO2MAX_HINT in writes
    assert not _captured(fake, "plotly_chart")


def test_render_prognosis_block_without_marathon_shape_does_not_crash(monkeypatch):
    """The sibling guard: a missing or unset `marathon.shape_pct` must render
    the table and skip the detail tiles, not `.dropna()` on `None`."""
    view = _runalyze_view()
    windowed = _prognosis_series(_prognosis_values())
    assert "marathon.shape_pct" not in windowed

    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    d.render_prognosis_block(windowed, view, "km")
    assert len(_captured(fake, "dataframe")) == 1
    assert not [a for n, a in fake.calls if n == "metric"]

    fake2 = _FakeSt()
    _patch_st(monkeypatch, fake2)
    d.render_prognosis_block({**windowed, "marathon.shape_pct": None}, view, "km")
    assert len(_captured(fake2, "dataframe")) == 1
    assert not [a for n, a in fake2.calls if n == "metric"]


def test_render_fitness_tab_calls_prognosis_block(monkeypatch):
    """Surface 2 call site: the prognosis block is invoked unconditionally from
    `render_fitness_tab` — deleting the call drops the table silently."""
    view = _runalyze_view()
    windowed = _runalyze_windowed(view)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    block = mock.Mock()
    monkeypatch.setattr(d, "render_prognosis_block", block)

    d.render_fitness_tab(view, windowed, "km")

    assert block.call_count == 1
    assert block.call_args.args == (windowed, view, "km")


class _MainSt:
    """Streamlit stub for a whole `main()` pass: widgets return usable values,
    containers are null context managers, calls are recorded."""

    def __init__(self, session_state):
        self.session_state = session_state
        self.calls = []

    @property
    def sidebar(self):
        return self

    def _rec(self, name, *args):
        self.calls.append((name, args))

    def radio(self, label, options, index=0, **kwargs):
        self._rec("radio", label, options)
        return options[index]

    def date_input(self, label, value=None, **kwargs):
        self._rec("date_input", label, value)
        return value

    def multiselect(self, label, options, default=None, **kwargs):
        self._rec("multiselect", label, options)
        return list(default or [])

    def button(self, *args, **kwargs):
        self._rec("button", *args)
        return False

    def tabs(self, labels):
        self._rec("tabs", labels)
        return [nullcontext() for _ in labels]

    def expander(self, *args, **kwargs):
        self._rec("expander", *args)
        return nullcontext()

    def __getattr__(self, name):
        def _rec(*args, **kwargs):
            self.calls.append((name, args))

        return _rec


def test_poll_store_changes_reruns_only_when_the_store_changed(monkeypatch):
    """Criterion 11: an open dashboard must reflect a scheduled refresh with no
    user action — the poll reruns the app when the store fingerprint moves."""
    state = {"_activities_fingerprint": "old"}
    fake = _MainSt(state)
    _patch_st(monkeypatch, fake)

    store = mock.Mock()
    store.activities_fingerprint.return_value = "old"
    monkeypatch.setattr(d, "get_store", lambda: store)

    assert d._store_changed(store, state) is False
    # The decorated fragment only runs inside a Streamlit session, so the
    # unwrapped body is what a test can drive.
    d.poll_store_changes.__wrapped__()
    assert not [c for c in fake.calls if c[0] == "rerun"]

    store.activities_fingerprint.return_value = "new"
    assert d._store_changed(store, state) is True
    d.poll_store_changes.__wrapped__()
    assert [c for c in fake.calls if c[0] == "rerun"]


def test_main_calls_paces_table_at_page_bottom(monkeypatch):
    """Surface 4 placement: `main()` must reach `render_paces_table` (bottom of
    Overview) last — dropping the call fails this test."""
    acts = _runalyze_acts()
    view = _runalyze_shape_only_view()
    _min_d, default_since, max_d = d.period_bounds([a.date for a in acts])

    state = {"activities": acts, "_details_period_key": (default_since, max_d)}
    fake = _MainSt(state)
    _patch_st(monkeypatch, fake)

    store = mock.Mock()
    store.load_runner_profile.return_value = RUNALYZE_PROFILE
    monkeypatch.setattr(d, "get_store", lambda: store)
    monkeypatch.setattr(d, "resolve_activities", lambda *a, **k: None)
    monkeypatch.setattr(d, "profile_from_widgets", lambda acts_, persisted: RUNALYZE_PROFILE)
    monkeypatch.setattr(d, "build_session_view", lambda *a, **k: view)
    monkeypatch.setattr(d, "persist_session_metrics", lambda *a, **k: None)
    for name in (
        "render_kpis",
        "render_load_tab",
        "render_fitness_tab",
        "render_volume_tab",
        "render_activities",
    ):
        monkeypatch.setattr(d, name, mock.Mock())

    paces = mock.Mock()
    monkeypatch.setattr(d, "render_paces_table", paces)

    d.main()

    assert paces.call_count == 1
    windowed, units = paces.call_args.args
    assert units == "km"
    assert windowed.get("vdot.shape") is not None
    # ...drawn under the Technical Reference expander (its markdown is the last
    # call before the table), i.e. at the page bottom.
    names = [c[0] for c in fake.calls]
    assert "expander" in names
    assert names.index("expander") < names.index("markdown", names.index("expander"))


# --- Garmin laps / splits panel (Task 5, criterion 7) ---


def _splits_fixture() -> dict:
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / "activity_splits.json").read_text())


def test_render_activity_laps_renders_table_from_cached_splits(monkeypatch, tmp_path):
    """A cached splits payload renders the laps table without touching the
    network; missing lap fields degrade to '—' and dynamics are averaged."""
    aid = 77
    (tmp_path / f"activity_splits_{aid}.json").write_text(json.dumps(_splits_fixture()))
    act = _coded_act(aid, "Interval Run", 10)
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(
        d, "GarminGateway", mock.Mock(side_effect=AssertionError("cache hit must not fetch"))
    )
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_activity_laps([act], aid, "km")

    dataframes = [a for name, a in fake.calls if name == "dataframe"]
    assert dataframes, "laps table not rendered"
    rows = dataframes[0][0]
    assert [r["lap"] for r in rows] == [1, 2, 3]
    lap1 = rows[0]
    assert lap1["distance"] == "1.00 km"
    assert lap1["time"] == "6:09"  # 368.653 s lap
    assert lap1["pace"] == "6:09"  # 1000 m in 368.653 s → 368.65 s/km
    assert lap1["avg HR"] == "124"
    missing = rows[1]
    assert missing["avg HR"] == "—" and missing["cadence"] == "—"
    dynamics = [a[0] for name, a in fake.calls if name == "caption" and "Running dynamics" in a[0]]
    assert dynamics and "avg cadence" in dynamics[0] and "stride" in dynamics[0]


def test_render_activity_laps_failed_fetch_degrades_to_caption(monkeypatch, tmp_path):
    """One dead splits fetch (auth failure, timeout, 404 without cache) shows a
    caption — never raises, never renders a half table."""
    aid = 88
    act = _coded_act(aid, "Tempo Run", 11)

    class _DeadGw:
        def __init__(self, cache_dir=None):
            self.cache_dir = cache_dir

        def fetch_activity_splits(self, activity_id):
            raise RuntimeError("dead splits endpoint")

    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _DeadGw)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_activity_laps([act], aid, "km")  # must not raise

    assert not any(name == "subheader" for name, _a in fake.calls)
    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert captions and "No lap data" in captions[0]


def test_render_activity_laps_empty_laps_renders_caption(monkeypatch, tmp_path):
    """A cached-but-lapless payload (e.g. a 404 cached as {}) shows the
    'no laps recorded' caption."""
    (tmp_path / "activity_splits_5.json").write_text("{}")
    act = _coded_act(5, "Easy Run", 12)
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_activity_laps([act], 5, "km")

    captions = [a[0] for name, a in fake.calls if name == "caption"]
    assert captions == ["No laps/splits recorded for this activity."]


def test_render_activity_laps_unknown_activity_silent(monkeypatch, tmp_path):
    """An id outside the visible list renders nothing and fetches nothing."""
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", mock.Mock(side_effect=AssertionError("no fetch ever")))
    fake = _FakeSt()
    _patch_st(monkeypatch, fake)

    d.render_activity_laps([_coded_act(6, "Easy Run", 13)], 999, "km")

    assert fake.calls == []


def test_render_activities_selected_row_calls_laps_panel(monkeypatch):
    """Selecting a row shows the activity's Garmin detail (laps panel), in
    addition to the repetition comparison."""
    acts = [_coded_act(91, "Winnetka - RF24 (Foundation Run)", 10)]
    calls = {}
    monkeypatch.setattr(
        d,
        "render_activity_laps",
        lambda activities, activity_id, units: calls.update(id=activity_id),
    )
    monkeypatch.setattr(d, "render_repetitions", mock.Mock())
    fake = _FakeSt()
    fake.dataframe = lambda *a, **k: mock.Mock(selection=mock.Mock(rows=[0]))
    _patch_st(monkeypatch, fake)

    d.render_activities(acts, date(2026, 8, 1), date(2026, 8, 31), "km", PROFILE_40M)

    assert calls["id"] == 91


def test_render_activities_rows_include_cadence_column(monkeypatch):
    """The flat activity log gains a cadence column (steps/min, '—' when old
    rows have no stored value)."""
    act = Activity(
        102,
        "running",
        date(2026, 1, 15),
        0,
        5000.0,
        1000.0,
        1000.0,
        150.0,
        170.0,
        avg_cadence=182.4,
    )
    nulled = Activity(
        103,
        "running",
        date(2026, 1, 20),
        0,
        5000.0,
        1000.0,
        1000.0,
        150.0,
        170.0,
    )
    _df, rows = _render_activities_rows(monkeypatch, [act, nulled], "km")
    assert rows["2026-01-15"]["cadence"] == "182"
    assert rows["2026-01-20"]["cadence"] == "—"


# --- degenerate chart attack -------------------------------------------------
# The chart renderers used to be only ever watched to succeed. These tests
# drive every charted tab with degenerate activity sets and a unit switch, and
# assert that nothing raises, that every chart still carries the shared
# template, and that no chart carries a non-finite value — with one known
# exception: the first bucket of the week-over-week charts has no predecessor,
# so its percentage change is NaN (true of normal data too).


_DEGENERATE_MODES = ["normal", "zero", "single", "outlier", "identical", "no_hr", "same_date"]

# The first week-over-week bucket has no previous week to compare against.
_ALLOWED_NAN_TITLES = ("Week-over-week distance change", "Week-over-week duration change")


def _degenerate_acts(mode):
    """Fixture activities rewritten into a degenerate shape."""
    import json as _json
    from pathlib import Path

    from normalize import from_summary

    raw = _json.loads((Path(__file__).parent / "fixtures" / "activities_sample.json").read_text())
    if mode == "single":
        raw = raw[:1]
    out = []
    for payload in raw:
        p = dict(payload)
        if mode == "zero":
            for key in (
                "distance",
                "duration",
                "elapsedDuration",
                "movingDuration",
                "averageSpeed",
                "maxSpeed",
                "elevationGain",
                "elevationLoss",
                "averageHR",
                "maxHR",
                "vO2MaxValue",
                "aerobicTrainingEffect",
                "anaerobicTrainingEffect",
                "averageRunningCadenceInStepsPerMinute",
                "steps",
                "calories",
            ):
                p[key] = 0
            p.pop("splitSummaries", None)
            p["lapCount"] = 0
        elif mode == "outlier":
            for key in (
                "distance",
                "duration",
                "elapsedDuration",
                "movingDuration",
                "elevationGain",
                "elevationLoss",
            ):
                p[key] = 1e9
            p["averageSpeed"] = 1e6
            p["maxSpeed"] = 1e6
            p["averageHR"] = 1000.0
            p["maxHR"] = 2000.0
            p["vO2MaxValue"] = 900.0
        elif mode == "identical":
            p.update(
                distance=5000.0,
                duration=1800.0,
                elapsedDuration=1800.0,
                movingDuration=1800.0,
                averageSpeed=2.78,
                maxSpeed=3.0,
                elevationGain=10.0,
                elevationLoss=10.0,
                averageHR=150.0,
                maxHR=160.0,
                vO2MaxValue=50.0,
                averageRunningCadenceInStepsPerMinute=180.0,
            )
        elif mode == "no_hr":
            for key in (
                "averageHR",
                "maxHR",
                "hrTimeInZone_1",
                "hrTimeInZone_2",
                "hrTimeInZone_3",
                "hrTimeInZone_4",
                "hrTimeInZone_5",
                "aerobicTrainingEffect",
                "anaerobicTrainingEffect",
                "vO2MaxValue",
            ):
                p.pop(key, None)
        elif mode == "same_date":
            # Every activity on one calendar day, with a start timestamp that
            # falls on another day (Garmin reports the athlete's local time).
            p["startTimeLocal"] = "2026-08-04 07:00:00"
            p["beginTimestamp"] = 1786000000000
        out.append(from_summary(p))
    return out


def _render_tabs(monkeypatch, acts, units):
    """Render every charted tab against a fake st; return the captured figures."""
    from profile import default_profile

    from session import build_session_view

    fake = _FakeSt()
    _patch_st(monkeypatch, fake)
    view = build_session_view(acts, default_profile(age=40, hrrest=60, sex="M"), None, None, None)
    since, until = date(2026, 6, 1), date(2026, 8, 31)
    windowed = view.windowed(since, until)
    d.render_kpis(windowed, view, units)
    d.render_load_tab(view, windowed, units)
    d.render_fitness_tab(view, windowed, units)
    d.render_prognosis_block(windowed, view, units)
    d.render_volume_tab(acts, view, windowed, units, since=since, until=until)
    d.render_paces_table(windowed, units)
    return [a[0] for name, a in fake.calls if name == "plotly_chart"]


def _assert_shared_layout(fig):
    """Every chart must be drawn through the shared template."""
    lay = fig.layout
    assert lay.template.layout.plot_bgcolor == "white", "chart is not on the shared template"
    assert lay.hovermode == "x unified"
    assert lay.legend.orientation == "h"
    assert (lay.margin.l, lay.margin.r, lay.margin.t, lay.margin.b) == (40, 20, 40, 40)


def _non_finite(fig):
    """Non-finite x/y values carried by a figure's traces."""
    bad = []
    for trace in fig.data:
        for axis in ("x", "y"):
            values = getattr(trace, axis, None)
            if values is None:
                continue
            for v in values:
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue  # dates / categoricals are not numbers
                if not math.isfinite(fv):
                    bad.append((axis, v))
    return bad


@pytest.mark.parametrize("mode", _DEGENERATE_MODES)
@pytest.mark.parametrize("units", ["km", "miles"])
def test_charts_survive_degenerate_activities(monkeypatch, mode, units):
    acts = _degenerate_acts(mode)
    figs = _render_tabs(monkeypatch, acts, units)
    for fig in figs:
        _assert_shared_layout(fig)
        title = str(getattr(fig.layout.title, "text", "") or "")
        bad = _non_finite(fig)
        if bad and not any(t in title for t in _ALLOWED_NAN_TITLES):
            raise AssertionError(f"{title!r} carries non-finite values: {bad}")


def test_unit_switch_changes_the_labels_but_not_the_chart_set(monkeypatch):
    """A metric↔miles switch re-labels the charts without changing which
    data (and therefore how many charts) exist."""
    acts = _degenerate_acts("normal")
    km_figs = _render_tabs(monkeypatch, acts, "km")
    mi_figs = _render_tabs(monkeypatch, acts, "miles")
    assert len(km_figs) == len(mi_figs)

    def titled(figs):
        return {str(getattr(f.layout.title, "text", "") or ""): f for f in figs}

    km = titled(km_figs)
    mi = titled(mi_figs)
    assert "Weekly distance" in km and "Weekly distance" in mi
    assert km["Weekly distance"].layout.yaxis2.title.text == "rolling km"
    assert mi["Weekly distance"].layout.yaxis2.title.text == "rolling mi"


def test_charts_keep_their_per_chart_overrides(monkeypatch):
    """The shared template must not swallow a chart's own layout."""
    figs = _render_tabs(monkeypatch, _degenerate_acts("normal"), "km")
    by_title = {str(getattr(f.layout.title, "text", "") or ""): f for f in figs}

    dist = by_title["Weekly distance"]
    assert dist.layout.barmode == "stack"
    assert dist.layout.hovermode == "x unified"

    hours = by_title["Weekly duration (hours)"]
    assert hours.layout.yaxis.title.text == "hours"

    wow = by_title["Week-over-week distance change"]
    assert wow.layout.yaxis.title.text == "%"
