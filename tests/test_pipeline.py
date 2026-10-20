# tests/test_pipeline.py
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from profile import RunnerProfile, default_profile

import pandas as pd
import pytest

from metric_series import rows_from_series
from metrics import effective_vo2max, marathon_shape, prognosis, vdot_shape
from normalize import Activity, from_summary
from pipeline import compute_metric_rows, run_pipeline
from store import MetricStore

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"


def test_end_to_end_writes_metrics(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    vo2 = json.loads((FIXTURE.parent / "vo2max_trend.json").read_text())
    db = tmp_path / "metrics.db"
    result = run_pipeline(
        acts, default_profile(age=40, hrrest=60, sex="M"), str(db), vo2max_payload=vo2
    )
    assert result["activities"] == len(acts)
    assert result["metrics_written"] > 0
    store = MetricStore(str(db))
    assert store.read_metric("volume.distance_total")
    assert store.read_metric("load.banister")
    assert store.read_metric("pmc.atl")
    assert store.read_metric("fitness.vo2max")
    # New per-sport + combined total load series now feed PMC/ACWR.
    assert store.read_metric("load.banister_running")
    assert store.read_metric("load.banister_treadmill")
    assert store.read_metric("load.banister_cross")
    assert store.read_metric("load.edwards")
    # Time-volume (hours/week) now exists alongside distance volume.
    assert store.read_metric("volume.duration_total")
    # ACWR needs a >=28-day span of HR-load activities (running + treadmill +
    # cross combined) to build the chronic window.
    load_days = {a.date for a in acts if a.sport in ("running", "treadmill", "cross")}
    span = (max(load_days) - min(load_days)).days
    assert bool(store.read_metric("load.acwr")) == (span >= 28)
    # HR load (combined running+treadmill+cross Banister) is emitted on days with
    # a load-bearing activity that has HR data; the per-sport rows land only on
    # days of that sport.
    hr_acts = [
        a for a in acts if a.sport in ("running", "treadmill", "cross") and a.avg_hr is not None
    ]
    hr_days = {a.date for a in hr_acts}
    for metric, sport in (
        ("load.banister_running", "running"),
        ("load.banister_treadmill", "treadmill"),
        ("load.banister_cross", "cross"),
    ):
        for row in store.read_metric(metric):
            assert pd.Timestamp(row["date"]).date() in {
                a.date for a in hr_acts if a.sport == sport
            }, f"{metric} row on {row['date']} has no {sport} HR activity"
    # combined load.banister only on combined load days
    for row in store.read_metric("load.banister"):
        assert pd.Timestamp(row["date"]).date() in hr_days, (
            f"load.banister row on {row['date']} has no HR-load activity"
        )
    params = json.loads(store.read_metric("load.banister_cross")[0]["params"])
    assert params["hrmax"] == 180
    flags = json.loads(store.read_metric("load.banister_cross")[0]["flags"])
    assert flags["basis"] == "cross_training"
    store.close()


def test_end_to_end_ingests_lt_and_race(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())
    race = json.loads((Path(__file__).parent / "fixtures" / "race_predictions.json").read_text())
    db = tmp_path / "metrics2.db"
    run_pipeline(
        acts, default_profile(age=40, hrrest=60, sex="M"), str(db), lt_payload=lt, race_payload=race
    )
    store = MetricStore(str(db))
    assert store.read_metric("load.lt_hr")
    assert store.read_metric("load.lt_pace")
    assert store.read_metric("race_5k")
    assert store.read_metric("load.cs_approx")
    store.close()


def test_end_to_end_ingests_vo2max_trend(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    vo2 = json.loads((FIXTURE.parent / "vo2max_trend.json").read_text())
    db = tmp_path / "metrics_vo2.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db), vo2max_payload=vo2)
    store = MetricStore(str(db))
    rows = store.read_metric("fitness.vo2max")
    assert rows
    assert any(r["value"] == 46.5 for r in rows)  # 2026-08-09 precise value
    assert any(r["value"] == 45.2 for r in rows)  # 2026-06-30 precise value
    store.close()


def test_compute_metric_rows_no_vo2_payload_no_fitness_metric():
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    rows = compute_metric_rows(acts, default_profile(age=40, hrrest=60, sex="M"))
    assert not [r for r in rows if r["metric"] == "fitness.vo2max"]


def test_end_to_end_ingests_live_race_schema(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())
    race = {
        "time5K": 1405,
        "time10K": 3076,
        "timeHalfMarathon": 7420,
        "timeMarathon": 17179,
        "calendarDate": "2026-08-28",
        "userId": 111267328,
    }
    db = tmp_path / "metrics_live.db"
    run_pipeline(
        acts, default_profile(age=40, hrrest=60, sex="M"), str(db), lt_payload=lt, race_payload=race
    )
    store = MetricStore(str(db))
    row = store.read_metric("race_5k")[0]
    assert row["value"] == 1405
    assert row["date"] == "2026-08-28"
    store.close()


def test_end_to_end_emits_cross_training_hr_load(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    db = tmp_path / "metrics_cross.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    store = MetricStore(str(db))
    cross_days = {a.date for a in acts if a.sport == "cross"}
    assert cross_days
    for metric in ("load.banister_cross", "load.edwards_cross"):
        rows = store.read_metric(metric)
        assert rows, f"{metric} missing"
        for row in rows:
            assert pd.Timestamp(row["date"]).date() in cross_days, (
                f"{metric} row on {row['date']} has no cross activity"
            )
    assert all(r["value"] > 0 for r in store.read_metric("load.banister_cross"))
    for a in acts:
        if a.date not in cross_days:
            for metric in ("load.banister_cross", "load.edwards_cross"):
                matches = [
                    r for r in store.read_metric(metric) if pd.Timestamp(r["date"]).date() == a.date
                ]
                assert not matches, f"{metric} emitted on non-cross day {a.date}"
    # every cross activity with an avg HR contributes a positive Banister load
    params = json.loads(store.read_metric("load.banister_cross")[0]["params"])
    assert params["hrmax"] == 180
    flags = json.loads(store.read_metric("load.banister_cross")[0]["flags"])
    assert flags["basis"] == "cross_training"
    store.close()


def _make_synthetic_details(n_samples: int, base_hr: float, base_speed: float):
    """Build a live-shape details dict with per-sample HR, speed, timestamps (1 Hz)."""
    return {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directTimestamp"},
            {"metricsIndex": 1, "key": "directSpeed"},
            {"metricsIndex": 2, "key": "directHeartRate"},
        ],
        "activityDetailMetrics": [
            {
                "metrics": [
                    float(i * 1000),
                    base_speed + (i % 10) * 0.01,
                    base_hr + (i % 10) * 0.5,
                ]
            }
            for i in range(n_samples)
        ],
    }


def test_pipeline_emits_best_not_rolling():
    """When lt_payload=None and series_by_id is provided, best-effort keys
    replace the old rolling keys."""
    from metrics.threshold import parse_details_series

    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    profile = default_profile(age=40, hrrest=60, sex="M")

    # Build series_by_id for two qualifying outdoor running activities
    # 24087730763: 4232s (>= 1800s for w30), 24135895959: 2082s (>= 1200s for w20)
    series_by_id = {}
    for act_id, n, hr_b, spd_b in [
        (24087730763, 2000, 150.0, 4.0),
        (24135895959, 2000, 148.0, 3.8),
    ]:
        det = _make_synthetic_details(n, hr_b, spd_b)
        hr_list, spd_list, ts_list = parse_details_series(det)
        series_by_id[act_id] = (hr_list, spd_list, ts_list)

    rows = compute_metric_rows(acts, profile, lt_payload=None, series_by_id=series_by_id)
    metrics = {r["metric"] for r in rows}

    # Best-effort anchors present
    assert "load.lt_hr_best20" in metrics, "lt_hr_best20 missing"
    assert "load.lt_hr_best30" in metrics, "lt_hr_best30 missing"
    assert "load.lt_pace_best20" in metrics, "lt_pace_best20 missing"
    assert "load.lt_pace_best30" in metrics, "lt_pace_best30 missing"
    assert "load.lt_effort_dots20_hr" in metrics, "dots20_hr missing"
    assert "load.lt_effort_dots20_pace" in metrics, "dots20_pace missing"
    assert "load.lt_effort_dots30_hr" in metrics, "dots30_hr missing"
    assert "load.lt_effort_dots30_pace" in metrics, "dots30_pace missing"

    # Old rolling keys absent
    assert "load.lt_hr_rolling" not in metrics, "old rolling HR still present"
    assert "load.lt_pace_rolling" not in metrics, "old rolling pace still present"

    # Verify anchor flags contain activity_id
    for r in rows:
        if r["metric"] == "load.lt_hr_best20":
            flags = json.loads(r["flags"])
            assert "activity_id" in flags
            assert flags["error_class"] == "best_effort_estimate"
            break

    # Pace rows are stored as speed in m/s (matching the unit param and the
    # Garmin load.lt_pace convention) — not s/m. Synthetic best is ~4 m/s.
    for r in rows:
        if r["metric"] == "load.lt_pace_best20":
            assert json.loads(r["params"])["unit"] == "m/s"
            assert 2.0 < r["value"] < 7.0, f"pace value {r['value']} is not m/s-scale"
            break
    else:
        raise AssertionError("load.lt_pace_best20 row missing")


def test_pipeline_no_series_by_id_no_best_effort():
    """When series_by_id is None (pure-activities path), no best-effort rows emitted."""
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    profile = default_profile(age=40, hrrest=60, sex="M")
    rows = compute_metric_rows(acts, profile, lt_payload=None, series_by_id=None)
    metrics = {r["metric"] for r in rows}
    assert not any("best" in m for m in metrics), "best-effort rows emitted without series_by_id"
    assert not any("dots" in m for m in metrics), "dots rows emitted without series_by_id"
    # Rolling keys should also be absent (rolling functions removed)
    assert "load.lt_hr_rolling" not in metrics
    assert "load.lt_pace_rolling" not in metrics


def test_pipeline_garmin_lt_suppresses_best_effort():
    """When Garmin LT payload has hr+pace, best-effort rows are NOT emitted."""
    from metrics.threshold import parse_details_series

    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    profile = default_profile(age=40, hrrest=60, sex="M")
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())

    series_by_id = {}
    for act_id, n, hr_b, spd_b in [
        (24087730763, 2000, 150.0, 4.0),
        (24135895959, 2000, 148.0, 3.8),
    ]:
        det = _make_synthetic_details(n, hr_b, spd_b)
        hr_list, spd_list, ts_list = parse_details_series(det)
        series_by_id[act_id] = (hr_list, spd_list, ts_list)

    rows = compute_metric_rows(acts, profile, lt_payload=lt, series_by_id=series_by_id)
    metrics = {r["metric"] for r in rows}
    # Garmin LT present → best-effort should NOT be emitted
    assert "load.lt_hr_best20" not in metrics
    assert "load.lt_hr_best30" not in metrics
    # But Garmin's own LT should be there
    assert "load.lt_hr" in metrics


def test_pipeline_emits_age_graded_pct():
    profile = RunnerProfile(hrmax=180, hrrest=60, sex="M", birth_year=1986, units="metric")
    acts = [
        # Eligible outdoor run: 5000m in 1000s at age 40 -> pct 0.77864
        Activity(1, "running", date(2026, 1, 10), 0, 5000.0, 1000.0, 1000.0, 150.0, 170.0),
        # Treadmill excluded; short run excluded.
        Activity(2, "treadmill", date(2026, 1, 11), 0, 5000.0, 900.0, 900.0, 150.0, 170.0),
        Activity(3, "running", date(2026, 1, 12), 0, 2500.0, 500.0, 500.0, 150.0, 170.0),
    ]
    rows = compute_metric_rows(acts, profile)
    ag = [r for r in rows if r["metric"] == "age_graded.pct"]
    assert len(ag) == 1
    assert ag[0]["date"] == "2026-01-10"
    assert ag[0]["value"] == pytest.approx(0.77864, rel=1e-4)
    assert ag[0]["source"] == "computed"
    params = json.loads(ag[0]["params"])
    assert params["unit"] == "fraction"
    assert params["basis"] == "wma_2023"


def test_pipeline_no_age_graded_without_eligible_runs():
    profile = RunnerProfile(hrmax=180, hrrest=60, sex="M", birth_year=1986, units="metric")
    acts = [
        Activity(1, "treadmill", date(2026, 1, 10), 0, 5000.0, 900.0, 900.0, 150.0, 170.0),
    ]
    rows = compute_metric_rows(acts, profile)
    assert [r for r in rows if r["metric"] == "age_graded.pct"] == []


def test_rows_from_series_keys():
    s = pd.Series([1.0, 2.5], index=pd.to_datetime(["2026-04-01", "2026-04-02"]))
    rows = rows_from_series("x", s, "computed", params={"a": 1}, flags={"b": 2})
    assert set(rows[0].keys()) == {"metric", "date", "value", "source", "params", "flags"}
    assert rows[0]["metric"] == "x"
    assert rows[1]["value"] == 2.5


def test_compute_metric_rows_matches_run_pipeline(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((FIXTURE.parent / "lactate_threshold.json").read_text())
    race = json.loads((FIXTURE.parent / "race_predictions.json").read_text())
    vo2 = json.loads((FIXTURE.parent / "vo2max_trend.json").read_text())

    rows = compute_metric_rows(acts, default_profile(age=40, hrrest=60, sex="M"), lt, race, vo2)
    assert rows

    db = tmp_path / "m.db"
    run_pipeline(
        acts,
        default_profile(age=40, hrrest=60, sex="M"),
        str(db),
        lt_payload=lt,
        race_payload=race,
        vo2max_payload=vo2,
    )
    store = MetricStore(str(db))
    stored = []
    for metric in {r["metric"] for r in rows}:
        stored += store.read_metric(metric)
    store.close()

    def norm(r):
        return (r["metric"], r["date"], r["value"], r["source"], r["params"], r["flags"])

    assert {norm(r) for r in rows} == {norm(r) for r in stored}


def test_compute_metric_rows_empty():
    rows = compute_metric_rows([], default_profile(age=40, hrrest=60, sex="M"))
    assert rows == []


def test_end_to_end_emits_strength_duration(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    strength_days = {a.date for a in acts if a.sport == "strength"}
    assert strength_days
    db = tmp_path / "metrics_strength.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    store = MetricStore(str(db))
    rows = store.read_metric("volume.duration_strength")
    assert rows, "volume.duration_strength missing despite strength activities"
    assert all(r["value"] > 0 for r in rows)
    # weekly duration hours sum to the strength activities' total hours
    total_h = sum(a.duration_s for a in acts if a.sport == "strength") / 3600.0
    assert sum(r["value"] for r in rows) == pytest.approx(total_h)
    # duration-only: strength never contributes HR load
    assert not store.read_metric("load.banister_strength")
    assert not store.read_metric("load.edwards_strength")
    for row in store.read_metric("load.banister"):
        assert pd.Timestamp(row["date"]).date() not in strength_days or pd.Timestamp(
            row["date"]
        ).date() in {a.date for a in acts if a.sport != "strength" and a.avg_hr is not None}, (
            f"load.banister row on {row['date']} driven by strength-only day"
        )
    store.close()


def _decoup_run(activity_id=101, day=date(2026, 5, 1), dur_s=6300.0):
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
    )


def _decoup_series(minutes=104):
    half = minutes // 2
    hr = [145.0] * half + [155.0] * (minutes - half)
    return hr, [3.0] * minutes, [i * 60000 for i in range(minutes)]


def test_pipeline_emits_decoupling_dot_for_eligible_run():
    a = _decoup_run()
    rows = compute_metric_rows(
        [a],
        default_profile(age=40, hrrest=60, sex="M"),
        series_by_id={a.activity_id: _decoup_series()},
    )
    dots = [r for r in rows if r["metric"] == "load.decoupling"]
    assert len(dots) == 1
    assert dots[0]["date"] == "2026-05-01"
    assert dots[0]["value"] == pytest.approx(155.0 / 145.0 - 1.0, rel=0.01)
    assert json.loads(dots[0]["params"])["unit"] == "fraction"
    flags = json.loads(dots[0]["flags"])
    assert flags["activity_id"] == 101
    assert "route_key" in flags


def test_pipeline_skips_short_run_and_missing_series():
    short = _decoup_run(activity_id=102, dur_s=1800.0)
    no_series = _decoup_run(activity_id=103, day=date(2026, 5, 2))
    rows = compute_metric_rows(
        [short, no_series],
        default_profile(age=40, hrrest=60, sex="M"),
        series_by_id={short.activity_id: _decoup_series()},
    )
    assert not [r for r in rows if r["metric"] == "load.decoupling"]


def test_pipeline_skips_interval_workout_for_decoupling():
    a = _decoup_run(activity_id=104)
    a.has_intervals = True
    rows = compute_metric_rows(
        [a],
        default_profile(age=40, hrrest=60, sex="M"),
        series_by_id={a.activity_id: _decoup_series()},
    )
    assert not [r for r in rows if r["metric"] == "load.decoupling"]


def test_pipeline_emits_cluster_mean_with_six_route_matched():
    acts = [
        _decoup_run(activity_id=200 + i, day=date(2026, 5, 1) + timedelta(days=i)) for i in range(6)
    ]
    series = {a.activity_id: _decoup_series() for a in acts}
    rows = compute_metric_rows(
        acts, default_profile(age=40, hrrest=60, sex="M"), series_by_id=series
    )
    means = [r for r in rows if r["metric"] == "load.decoupling_mean"]
    assert len(means) == 1
    assert means[0]["value"] == pytest.approx(155.0 / 145.0 - 1.0, rel=0.01)
    assert json.loads(means[0]["params"])["n"] == 6


def test_pipeline_no_cluster_mean_below_six_sessions():
    acts = [
        _decoup_run(activity_id=300 + i, day=date(2026, 5, 1) + timedelta(days=i)) for i in range(3)
    ]
    series = {a.activity_id: _decoup_series() for a in acts}
    rows = compute_metric_rows(
        acts, default_profile(age=40, hrrest=60, sex="M"), series_by_id=series
    )
    assert len([r for r in rows if r["metric"] == "load.decoupling"]) == 3
    assert not [r for r in rows if r["metric"] == "load.decoupling_mean"]


def test_pipeline_emits_daily_volume_rows(tmp_path):
    """Daily distance and duration rows are emitted alongside weekly rows."""
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    db = tmp_path / "metrics_daily.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    store = MetricStore(str(db))
    # Daily distance total exists
    daily_dist = store.read_metric("volume.distance_total_daily")
    assert daily_dist, "volume.distance_total_daily missing"
    # Daily duration total exists
    daily_dur = store.read_metric("volume.duration_total_daily")
    assert daily_dur, "volume.duration_total_daily missing"
    # Weekly distance total still exists (unchanged)
    weekly_dist = store.read_metric("volume.distance_total")
    assert weekly_dist, "volume.distance_total weekly missing"
    # Weekly duration total still exists (unchanged)
    weekly_dur = store.read_metric("volume.duration_total")
    assert weekly_dur, "volume.duration_total weekly missing"
    store.close()


def test_pipeline_daily_volume_consistency_with_weekly(tmp_path):
    """Daily volume rows sum to the corresponding weekly row for the same week."""
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    db = tmp_path / "metrics_consistency.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    store = MetricStore(str(db))
    # Get weekly distance total rows
    weekly_rows = store.read_metric("volume.distance_total")
    # Group weekly rows by date, find a week with data
    for row in weekly_rows:
        row_date = pd.Timestamp(row["date"])
        week_start = row_date - pd.Timedelta(days=row_date.weekday())  # Monday
        week_end = week_start + pd.Timedelta(days=6)  # Sunday
        # Sum daily distance rows that fall in this week
        daily_rows = store.read_metric("volume.distance_total_daily")
        week_daily_sum = sum(
            r["value"] for r in daily_rows if week_start <= pd.Timestamp(r["date"]) <= week_end
        )
        # The weekly value should equal the sum of its daily components
        assert row["value"] == pytest.approx(week_daily_sum), (
            f"Weekly distance {row['value']} != daily sum {week_daily_sum} for week starting {week_start.date()}"
        )
    store.close()


def test_pipeline_daily_volume_strength_absent_distance_present_duration():
    """strength group: distance_daily is empty (no running), duration_daily exists."""
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    # Filter to only strength activities
    strength_acts = [a for a in acts if a.sport == "strength"]
    assert strength_acts, "Need strength activities for this test"
    rows = compute_metric_rows(strength_acts, default_profile(age=40, hrrest=60, sex="M"))
    metrics = {r["metric"] for r in rows}
    # distance_strength_daily should be absent (no running distance for strength)
    assert "volume.distance_strength_daily" not in metrics, (
        "distance_strength_daily should be absent for strength-only activities"
    )
    # duration_strength_daily should be present
    assert "volume.duration_strength_daily" in metrics, (
        "duration_strength_daily should be present for strength activities"
    )


# --- Runalyze VDOT chain wiring (Task 8) ----------------------------------

PROFILE_40M_VDOT = RunnerProfile(hrmax=180, hrrest=60, sex="M", birth_year=1986)

PROGNOSIS_RAW_KEYS = {
    "prognosis.1",
    "prognosis.3",
    "prognosis.5",
    "prognosis.10",
    "prognosis.21.1",
    "prognosis.42.2",
    "prognosis.50",
}


def _vdot_run(
    activity_id,
    day,
    dist_m=10000.0,
    dur_s=3000.0,
    hr=150.0,
    *,
    sport="running",
    event_type=None,
    elapsed_s=None,
):
    """Qualifying synthetic run for the Runalyze wiring tests.

    elapsed_s defaults to dur_s; pass a larger value to make the moving-vs-
    elapsed distinction observable.
    """
    return Activity(
        activity_id=activity_id,
        sport=sport,
        date=day,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=dur_s,
        elapsed_s=dur_s if elapsed_s is None else elapsed_s,
        avg_hr=hr,
        max_hr=hr + 5,
        event_type=event_type,
    )


def test_pipeline_emits_vdot_shape_with_qualifying_runs():
    acts = [
        _vdot_run(1, date(2026, 1, 10)),
        _vdot_run(2, date(2026, 1, 12)),
        _vdot_run(3, date(2026, 1, 14)),
        _vdot_run(4, date(2026, 1, 15), sport="treadmill"),
        _vdot_run(5, date(2026, 1, 16), sport="cross"),
    ]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)

    shape = [r for r in rows if r["metric"] == "vdot.shape"]
    assert [r["date"] for r in shape] == [f"2026-01-{d:02d}" for d in range(10, 17)]
    assert all(r["source"] == "computed" for r in shape)

    expected = vdot_shape.shape_series(acts, 180.0, use_elevation=True)
    for r in shape:
        assert r["value"] == pytest.approx(expected[pd.Timestamp(r["date"])])

    params = json.loads(shape[0]["params"])
    assert params["window_days"] == 30
    flags = json.loads(shape[0]["flags"])
    assert flags["corrector"] == 1.0
    assert flags["hrmax_source"] == PROFILE_40M_VDOT.hrmax_source

    # marathon + every prognosis variant ride the same day set
    for metric in (
        ("marathon.shape_pct",)
        + tuple(PROGNOSIS_RAW_KEYS)
        + tuple(f"{k}_damped" for k in PROGNOSIS_RAW_KEYS)
    ):
        assert [r["date"] for r in rows if r["metric"] == metric] == [r["date"] for r in shape], (
            f"{metric} day set diverges from vdot.shape"
        )


def test_pipeline_omits_prognosis_when_no_qualifying():
    """Only strength/cross activities: no shape, no marathon, no prognosis."""
    acts = [
        Activity(1, "strength", date(2026, 1, 10), 0, 0.0, 3600.0, 3600.0, 120.0, 140.0),
        Activity(2, "cross", date(2026, 1, 11), 0, 0.0, 3000.0, 3000.0, 130.0, 150.0),
    ]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    metrics = {r["metric"] for r in rows}
    assert "vdot.shape" not in metrics
    assert "marathon.shape_pct" not in metrics
    assert not any(m.startswith("prognosis.") for m in metrics)


def test_pipeline_emits_exact_seven_prognosis_keys():
    acts = [
        _vdot_run(1, date(2026, 1, 10)),
        _vdot_run(2, date(2026, 1, 12)),
        _vdot_run(3, date(2026, 1, 14)),
    ]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    metrics = {r["metric"] for r in rows}
    raw = {m for m in metrics if m.startswith("prognosis.") and not m.endswith("_damped")}
    damped = {m for m in metrics if m.endswith("_damped")}
    assert raw == PROGNOSIS_RAW_KEYS
    assert damped == {f"{k}_damped" for k in PROGNOSIS_RAW_KEYS}
    # A plain f"prognosis.{d}" renders float distances as 1.0/10.0/50.0.
    assert "prognosis.1.0" not in metrics
    assert "prognosis.10.0" not in metrics
    assert "prognosis.50.0" not in metrics

    # The prognosis is the Daniels-Gilbert inverse of that day's corrected
    # shape; the damped twin feeds the day's marathon shape into the factor.
    shape = {r["date"]: r["value"] for r in rows if r["metric"] == "vdot.shape"}
    pct = {r["date"]: r["value"] for r in rows if r["metric"] == "marathon.shape_pct"}
    for r in rows:
        if r["metric"] == "prognosis.5":
            assert r["value"] == pytest.approx(prognosis.prognosis_seconds(shape[r["date"]], 5.0))
    damped_42 = {r["date"]: r["value"] for r in rows if r["metric"] == "prognosis.42.2_damped"}
    for r in rows:
        if r["metric"] != "prognosis.42.2":
            continue
        day = r["date"]
        assert damped_42[day] == pytest.approx(
            prognosis.prognosis_seconds(shape[day], 42.2, marathon_shape_pct=pct[day])
        )
        assert damped_42[day] > r["value"], "marathon-shape damping must slow the marathon"


def test_pipeline_vdot_shape_skips_empty_window_days():
    """A gap longer than the 30-day window emits no rows at all (never 0.0)."""
    acts = [_vdot_run(1, date(2026, 1, 1)), _vdot_run(2, date(2026, 3, 5))]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)

    shape_dates = [r["date"] for r in rows if r["metric"] == "vdot.shape"]
    assert shape_dates[0] == "2026-01-01"
    assert shape_dates[-1] == "2026-03-05"
    # Run 1 (noon 01-01) lights the trailing-window days 01-01..01-30; run 2
    # (noon 03-05) only lights its own day, since the series stops there.
    assert "2026-01-30" in shape_dates
    assert "2026-01-31" not in shape_dates
    assert "2026-02-02" not in shape_dates
    assert "2026-03-04" not in shape_dates
    assert "2026-03-05" in shape_dates
    assert all(r["value"] > 0 for r in rows if r["metric"] == "vdot.shape")
    assert [r["date"] for r in rows if r["metric"] == "marathon.shape_pct"] == shape_dates
    assert [r["date"] for r in rows if r["metric"] == "prognosis.5"] == shape_dates


def test_pipeline_survives_activity_whose_timestamp_leaves_its_calendar_day():
    """Regression: the shape window filter can empty the series.

    Garmin reports the athlete's local time, so an activity's start timestamp
    can land on a different calendar day than the payload's date (timezone
    change, travel). The 30-day window filter then drops that day, and the
    prognosis block used to index the emptied series — a whole-dashboard
    IndexError.
    """
    stamp = int(datetime(2026, 8, 6, 12, tzinfo=UTC).timestamp() * 1000)
    acts = [
        replace(_vdot_run(1, date(2026, 8, 4)), ts_ms=stamp),
        replace(_vdot_run(2, date(2026, 8, 4)), ts_ms=stamp),
    ]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)

    assert rows  # the other metric families still land
    # No day survived the window filter, so there is nothing to shape or prognose.
    assert [r for r in rows if r["metric"] == "vdot.shape"] == []
    assert [r for r in rows if r["metric"] == "marathon.shape_pct"] == []
    assert [r for r in rows if r["metric"].startswith("prognosis.")] == []


def test_pipeline_marathon_targets_resolve_from_latest_shape():
    """Targets resolve from the current shape for every as-of day (spec 6)."""
    acts = [
        _vdot_run(1, date(2026, 1, 1), 25000.0, 12000.0, 140.0),
        _vdot_run(2, date(2026, 3, 5), 25000.0, 5000.0, 175.0),
    ]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    shape = {r["date"]: r["value"] for r in rows if r["metric"] == "vdot.shape"}
    pct = {r["date"]: r["value"] for r in rows if r["metric"] == "marathon.shape_pct"}
    latest = shape["2026-03-05"]

    early_day = date(2026, 1, 1)
    runs_as_of_early = [
        ((early_day - a.date).days, a.distance_km) for a in acts if a.date <= early_day
    ]
    days_for_week_km = marathon_shape.clamped_week_days(
        (early_day - min(a.date for a in acts)).days
    )

    from_latest = marathon_shape.percentage(
        runs_as_of_early, latest, days_for_week_km=days_for_week_km
    )
    from_early = marathon_shape.percentage(
        runs_as_of_early, shape["2026-01-01"], days_for_week_km=days_for_week_km
    )
    assert from_latest != from_early, "test data does not discriminate the two readings"
    assert pct["2026-01-01"] == from_latest


def test_corrector_factor_comes_from_race_tagged_activities():
    acts = [
        _vdot_run(1, date(2026, 1, 10)),
        _vdot_run(2, date(2026, 1, 12)),
        _vdot_run(3, date(2026, 1, 14)),
        _vdot_run(
            4, date(2026, 1, 20), 10000.0, 2400.0, 178.0, event_type="race", elapsed_s=3000.0
        ),
        _vdot_run(5, date(2026, 1, 27), 5000.0, 1140.0, 178.0, event_type="race"),
    ]
    races = [a for a in acts if a.event_type == "race"]
    factor = effective_vo2max.corrector_factor(
        [effective_vo2max.race_ratio(a.distance_km, a.duration_s, a.avg_hr / 180.0) for a in races],
        race_vdots=[effective_vo2max.race_vdot_by_time(a.distance_km, a.duration_s) for a in races],
    )
    assert factor != pytest.approx(1.0)

    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    got = {r["date"]: r["value"] for r in rows if r["metric"] == "vdot.shape"}
    raw = vdot_shape.shape_series(acts, 180.0, use_elevation=True)
    assert len(got) == len(raw)
    for day in raw.index:
        assert got[day.strftime("%Y-%m-%d")] == pytest.approx(raw[day] * factor, rel=1e-4)

    # Moving time is the input: the race ratio changes when elapsed time is used.
    race4 = races[0]
    ratio_moving = effective_vo2max.race_ratio(race4.distance_km, race4.duration_s, 178.0 / 180.0)
    ratio_elapsed = effective_vo2max.race_ratio(race4.distance_km, race4.elapsed_s, 178.0 / 180.0)
    assert ratio_moving != pytest.approx(ratio_elapsed)

    # Without any race tag the corrector stays neutral.
    untagged = [
        _vdot_run(1, date(2026, 1, 10)),
        _vdot_run(2, date(2026, 1, 12)),
        _vdot_run(3, date(2026, 1, 14)),
    ]
    plain = {
        r["date"]: r["value"]
        for r in compute_metric_rows(untagged, PROFILE_40M_VDOT)
        if r["metric"] == "vdot.shape"
    }
    raw_plain = vdot_shape.shape_series(untagged, 180.0, use_elevation=True)
    for day in raw_plain.index:
        assert plain[day.strftime("%Y-%m-%d")] == pytest.approx(raw_plain[day])


def test_pipeline_corrector_is_not_floored_at_one():
    """A low-HR race can push the corrector below 1.0; the raw max is kept."""
    acts = [
        _vdot_run(1, date(2026, 1, 20), 10000.0, 2400.0, 120.0, event_type="race"),
        _vdot_run(2, date(2026, 1, 22)),
    ]
    factor = effective_vo2max.corrector_factor(
        [effective_vo2max.race_ratio(10.0, 2400.0, 120.0 / 180.0)],
        race_vdots=[effective_vo2max.race_vdot_by_time(10.0, 2400.0)],
    )
    assert factor < 1.0

    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    got = {r["date"]: r["value"] for r in rows if r["metric"] == "vdot.shape"}
    raw = vdot_shape.shape_series(acts, 180.0, use_elevation=True)
    for day in raw.index:
        assert got[day.strftime("%Y-%m-%d")] == pytest.approx(raw[day] * factor, rel=1e-4)


def test_pipeline_omits_prognosis_when_shape_out_of_range():
    """A shape below 15 is not a reasonable vdot: no prognosis rows, no filler."""
    acts = [_vdot_run(1, date(2026, 1, 10), 5000.0, 6000.0, 180.0)]
    rows = compute_metric_rows(acts, PROFILE_40M_VDOT)
    metrics = {r["metric"] for r in rows}
    assert "vdot.shape" in metrics
    assert "marathon.shape_pct" in metrics
    assert not any(m.startswith("prognosis.") for m in metrics)
