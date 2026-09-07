# tests/test_pipeline.py
import json
from pathlib import Path
from profile import default_profile

import pandas as pd
import pytest

from metric_series import rows_from_series
from normalize import from_summary
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
