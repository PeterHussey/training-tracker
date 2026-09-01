# tests/test_pipeline.py
import json
from pathlib import Path
from profile import default_profile

import pandas as pd

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
