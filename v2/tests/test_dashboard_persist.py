"""Dashboard persistence: computed view metrics (incl. race predictions) must reach the store.

Reproduces the reported bug: dashboard refresh only called
store.save_activities(), so metric_series stayed empty and race_* rows were
only ever in-memory (session_state). Querying the SQLite store showed no
race data, and every app restart lost the predictions.
"""

import json
from pathlib import Path
from profile import default_profile

from dashboard import CACHE_DIR_NAME, load_cached_trends
from normalize import from_summary
from pipeline import persist_session_metrics
from store import MetricStore

FIXTURES = Path(__file__).parent / "fixtures"


def test_persist_session_metrics_writes_race_rows(tmp_path):
    acts = [from_summary(a) for a in json.loads((FIXTURES / "activities_sample.json").read_text())]
    race = json.loads((FIXTURES / "race_predictions.json").read_text())
    lt = json.loads((FIXTURES / "lactate_threshold.json").read_text())
    vo2 = json.loads((FIXTURES / "vo2max_trend.json").read_text())

    db = tmp_path / "persist.db"
    store = MetricStore(str(db))
    store.save_activities(acts)

    # This is what the dashboard must do after building its session view:
    # persist every computed row (including ingested race predictions).
    persist_session_metrics(
        store,
        acts,
        default_profile(age=40, hrrest=60, sex="M"),
        lt_payload=lt,
        race_payload=race,
        vo2max_payload=vo2,
    )

    assert store.read_metric("race_5k"), "race_5k missing from store after persist"
    assert store.read_metric("race_10k")
    assert store.read_metric("race_half")
    assert store.read_metric("race_full")
    assert store.read_metric("volume.distance_total")
    store.close()


def test_persist_session_metrics_live_race_schema(tmp_path):
    acts = [from_summary(a) for a in json.loads((FIXTURES / "activities_sample.json").read_text())]
    race = {
        "time5K": 1405,
        "time10K": 3076,
        "timeHalfMarathon": 7420,
        "timeMarathon": 17179,
        "calendarDate": "2026-08-28",
        "userId": 111267328,
    }
    db = tmp_path / "persist_live.db"
    store = MetricStore(str(db))

    persist_session_metrics(
        store,
        acts,
        default_profile(age=40, hrrest=60, sex="M"),
        race_payload=race,
    )

    row = store.read_metric("race_5k")[0]
    assert row["value"] == 1405
    assert row["date"] == "2026-08-28"
    store.close()


def test_load_cached_trends_rehydrates_race_payload(tmp_path):
    cache = tmp_path / CACHE_DIR_NAME
    cache.mkdir()
    (cache / "race_predictions.json").write_text('{"time5K": 1413, "calendarDate": "2026-09-03"}')
    (cache / "lactate_threshold.json").write_text('{"speed_and_heart_rate": {}}')

    lt, race, vo2 = load_cached_trends(cache)
    assert race is not None and race["time5K"] == 1413
    assert lt == {"speed_and_heart_rate": {}}
    assert vo2 is None  # no vo2 trend file cached


def test_load_cached_trends_missing_dir_returns_nones(tmp_path):
    lt, race, vo2 = load_cached_trends(tmp_path / "does_not_exist")
    assert (lt, race, vo2) == (None, None, None)


def test_persist_session_metrics_writes_race_trend(tmp_path):
    acts = [from_summary(a) for a in json.loads((FIXTURES / "activities_sample.json").read_text())]
    trend = [
        {
            "calendarDate": "2026-08-28",
            "time5K": 1405,
            "time10K": 3076,
            "timeHalfMarathon": 7420,
            "timeMarathon": 17179,
        },
        {
            "calendarDate": "2026-09-03",
            "time5K": 1413,
            "time10K": 3100,
            "timeHalfMarathon": 7460,
            "timeMarathon": 17267,
        },
    ]
    db = tmp_path / "persist_trend.db"
    store = MetricStore(str(db))

    persist_session_metrics(
        store,
        acts,
        default_profile(age=40, hrrest=60, sex="M"),
        race_payload=trend,
    )

    rows = store.read_metric("race_5k")
    assert [(r["date"], r["value"]) for r in rows] == [
        ("2026-08-28", 1405.0),
        ("2026-09-03", 1413.0),
    ]
    assert len(store.read_metric("race_full")) == 2
    store.close()


def test_load_cached_trends_prefers_daily_trend_over_latest(tmp_path):
    cache = tmp_path / CACHE_DIR_NAME
    cache.mkdir()
    (cache / "race_predictions.json").write_text('{"time5K": 1413, "calendarDate": "2026-09-03"}')
    (cache / "race_predictions_trend_2025-09-01_2026-09-01.json").write_text(
        '[{"calendarDate": "2026-08-28", "time5K": 1405}]'
    )
    (cache / "lactate_threshold.json").write_text('{"speed_and_heart_rate": {}}')
    (cache / "vo2max_trend_2025-09-01_2026-09-01.json").write_text(
        '[{"generic": {"calendarDate": "2026-08-29", "vo2MaxPreciseValue": 46.5}}]'
    )

    lt, race, vo2 = load_cached_trends(cache)
    assert race == [{"calendarDate": "2026-08-28", "time5K": 1405}]
    assert lt == {"speed_and_heart_rate": {}}
    assert vo2 and vo2[0]["generic"]["vo2MaxPreciseValue"] == 46.5
