# tests/test_pipeline.py
import json
from pathlib import Path

import pandas as pd

from metric_series import rows_from_series
from normalize import from_summary
from pipeline import run_pipeline
from profile import default_profile
from store import MetricStore

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"


def test_end_to_end_writes_metrics(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    db = tmp_path / "metrics.db"
    result = run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    assert result["activities"] == len(acts)
    assert result["metrics_written"] > 0
    store = MetricStore(str(db))
    assert store.read_metric("volume.distance_total")
    assert store.read_metric("load.banister")
    assert store.read_metric("pmc.atl")
    assert store.read_metric("fitness.vo2max")
    # ACWR needs a 28-day chronic window over the running-only load series;
    # the fixture's running span decides whether it exists.
    running_days = {a.date for a in acts if a.sport == "running"}
    span = (max(running_days) - min(running_days)).days
    assert bool(store.read_metric("load.acwr")) == (span >= 28)
    # Global Constraint: HR load/PMC/ACWR are computed on outdoor-running
    # activities only. Every load row must land on a day with a running activity,
    # and no load row may exist on a day whose only activities are
    # treadmill/cycling/strength (cross-training volume only).
    for metric in ("load.banister", "load.edwards"):
        for row in store.read_metric(metric):
            assert pd.Timestamp(row["date"]).date() in running_days, (
                f"{metric} row on {row['date']} has no outdoor running activity")
    for a in acts:
        if a.date not in running_days:
            for metric in ("load.banister", "load.edwards"):
                matches = [r for r in store.read_metric(metric)
                           if pd.Timestamp(r["date"]).date() == a.date]
                assert not matches, (
                    f"{metric} emitted on {a.date} ({a.sport}) — non-running day")
    store.close()


def test_end_to_end_ingests_lt_and_race(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())
    race = json.loads((Path(__file__).parent / "fixtures" / "race_predictions.json").read_text())
    db = tmp_path / "metrics2.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db),
                 lt_payload=lt, race_payload=race)
    store = MetricStore(str(db))
    assert store.read_metric("load.lt_hr")
    assert store.read_metric("load.lt_pace")
    assert store.read_metric("race_5k")
    assert store.read_metric("load.cs_approx")
    store.close()


def test_end_to_end_ingests_live_race_schema(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())
    race = {
        "time5K": 1405, "time10K": 3076, "timeHalfMarathon": 7420, "timeMarathon": 17179,
        "calendarDate": "2026-08-28", "userId": 111267328,
    }
    db = tmp_path / "metrics_live.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db),
                 lt_payload=lt, race_payload=race)
    store = MetricStore(str(db))
    row = store.read_metric("race_5k")[0]
    assert row["value"] == 1405
    assert row["date"] == "2026-08-28"
    store.close()


def test_rows_from_series_keys():
    s = pd.Series([1.0, 2.5], index=pd.to_datetime(["2026-04-01", "2026-04-02"]))
    rows = rows_from_series("x", s, "computed", params={"a": 1}, flags={"b": 2})
    assert set(rows[0].keys()) == {"metric", "date", "value", "source", "params", "flags"}
    assert rows[0]["metric"] == "x"
    assert rows[1]["value"] == 2.5