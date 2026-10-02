"""Tests for dashboard refresh window logic.

Root cause of the recurring timeout: `refresh_garmin` re-paginated the entire
365-day history every run (~85 100-row pages, >90s) instead of only new pages.
These tests pin the incremental-fetch behavior: subsequent refreshes narrow the
Garmin window to activities newer than the latest already in the local store,
while the first refresh (empty store) keeps the full window.
"""

import json
import time
from datetime import date, timedelta
from profile import RunnerProfile
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


def test_render_kpis_lt_hr_falls_back_to_best20(monkeypatch):
    """No Garmin LT record: the LT HR tile shows the 20-min best-effort
    anchor, clearly labeled, instead of a dash."""
    view = _decoupling_view()
    assert "load.lt_hr" not in view.series
    windowed = view.windowed(date(2026, 7, 1), date(2026, 7, 31))
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)
    d.render_fitness_tab(view, windowed, "km")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert any("Age-graded" in str(getattr(f.layout.title, "text", "")) for f in figs)


def test_volume_tab_sidebar_agg_toggle_weekly_default(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)
    acts = [
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
    ]
    d.render_volume_tab(acts, view, windowed, "km", "monthly")
    figs = [a[0] for name, a in fake.calls if name == "plotly_chart"]
    assert figs and "Monthly" in str(figs[0].layout.title.text)


def test_volume_tab_no_rollup_pane(monkeypatch):
    view = _graded_view()
    windowed = view.windowed(date(2026, 1, 1), date(2026, 1, 31))
    fake = _FakeSt()
    # Ensure radio is not called at all
    fake.radio = lambda *a, **k: None  # return falsy so widget value is ignored
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)
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
    monkeypatch.setattr(d, "st", fake)

    d.render_repetitions(acts, 2, "km")

    assert fetched == [2]  # id 1 served from disk cache, id 3 is another code
    # both reps produced a pace in row data: disk-cached 3.0 m/s, fetched 4.0 m/s
    assert len(pace_calls) == 2
    (v1, u1), (v2, u2) = pace_calls
    assert (v1, u1) == (pytest.approx(1000.0 / 3.0 / 60.0), "km")
    assert (v2, u2) == (pytest.approx(1000.0 / 4.0 / 60.0), "km")
    # the value rides in the row data only — the Fast30s column render is Task 5's
    shown = [a for name, a in fake.calls if name == "dataframe"][0][0]
    assert shown and all("fast30s" not in row for row in shown)


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
    monkeypatch.setattr(d, "st", fake)

    d.render_repetitions(acts, 12, "km")  # must not raise

    assert sorted(fetched) == [11, 12]  # both attempted; the dead one didn't abort the rest
    assert len(pace_calls) == 1  # only the healthy rep produced a pace value
    assert pace_calls[0] == (pytest.approx(1000.0 / 4.0 / 60.0), "km")


def test_render_repetitions_fetches_only_outdoor_running_reps(monkeypatch, tmp_path):
    """Strength/cross reps have no meaningful pace: never fetched, value None."""
    acts = [
        _coded_act(21, "Winnetka - RF24 (Foundation Run)", 10, sport="running"),
        _coded_act(22, "Winnetka - RF24 (Foundation Run)", 15, sport="strength"),
    ]
    fetched = []
    monkeypatch.setattr(d, "APP_CACHE_DIR", tmp_path)
    monkeypatch.setattr(d, "GarminGateway", _rep_gateway(fetched))
    pace_calls = _record_pace(monkeypatch)
    fake = _FakeSt()
    monkeypatch.setattr(d, "st", fake)

    d.render_repetitions(acts, 21, "km")

    assert fetched == [21]  # the strength rep's details are never requested
    assert len(pace_calls) == 1  # only the outdoor run got a pace value


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
    monkeypatch.setattr(d, "st", fake)

    d.render_activities(acts, date(2026, 8, 1), date(2026, 8, 31), "km", PROFILE_40M)

    gw_factory.assert_not_called()
