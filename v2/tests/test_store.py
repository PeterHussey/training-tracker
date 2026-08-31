# tests/test_store.py
import json
import threading
from datetime import date

import pytest

from normalize import Activity
from profile import default_profile
from store import MetricStore


ACT = Activity(activity_id=1, sport="running", date=date(2026, 4, 1), ts_ms=0,
               distance_m=10000.0, duration_s=3600.0, elapsed_s=3600.0, avg_hr=150.0,
               max_hr=170.0, zone_s={1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0},
               ele_gain_m=50.0, vo2max=46.0)


def test_store_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
    store.save_activities([ACT])
    store.save_metric_rows([{
        "metric": "load.banister", "date": "2026-04-01", "value": 120.5,
        "source": "computed", "params": "{}", "flags": "{}",
    }])
    rows = store.read_metric("load.banister")
    assert rows[0]["value"] == 120.5
    assert rows[0]["source"] == "computed"
    store.close()


def test_schema_has_required_tables(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"runner_profile", "activities", "metric_series"} <= tables
    store.close()


from profile import RunnerProfile


SECOND_ACT = Activity(
    activity_id=2, sport="cross", date=date(2026, 4, 2), ts_ms=0,
    distance_m=0.0, duration_s=2700.0, elapsed_s=2700.0, avg_hr=140.0,
    max_hr=160.0, zone_s={1: 400.0, 2: 800.0}, vo2max=None, ele_gain_m=None,
)


def test_load_activities_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([ACT, SECOND_ACT])
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 2
    first, second = loaded[0], loaded[1]
    assert first.activity_id == 1
    assert first.sport == "running"
    assert first.date == date(2026, 4, 1)
    assert first.distance_m == 10000.0
    assert first.duration_s == 3600.0
    assert first.avg_hr == 150.0
    assert first.max_hr == 170.0
    assert first.vo2max == 46.0
    assert first.ele_gain_m == 50.0
    assert first.zone_s == {1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0}
    assert second.sport == "cross"
    assert second.avg_hr == 140.0
    assert second.vo2max is None


def test_load_activities_empty(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_activities() == []
    store.close()


def test_load_runner_profile_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = default_profile(age=40, hrrest=60, sex="M")  # age-predicted hrmax 180 + zones
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == p.hrmax == 180
    assert loaded.hrmax_source == "age_predicted"
    assert loaded.hrrest == 60
    assert loaded.sex == "M"
    assert loaded.birth_year == p.birth_year
    assert loaded.hr_zones == p.hr_zones
    assert loaded.units == "metric"


def test_load_runner_profile_configured_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(hrmax=185, hrrest=62, sex="F", birth_year=1990, lthr_manual=168,
                      hrmax_source="configured")
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == 185
    assert loaded.hrmax_source == "configured"
    assert loaded.sex == "F"
    assert loaded.birth_year == 1990
    assert loaded.lthr_manual == 168


def test_load_runner_profile_none(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_runner_profile() is None
    store.close()


def test_load_runner_profile_nondefault_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(hrmax=190, hrrest=55, sex="F", birth_year=1985,
                      units="imperial", hrmax_source="configured",
                      lthr_manual=172)
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == 190
    assert loaded.hrrest == 55
    assert loaded.sex == "F"
    assert loaded.birth_year == 1985
    assert loaded.units == "imperial"
    assert loaded.hrmax_source == "configured"
    assert loaded.lthr_manual == 172


def test_store_is_usable_from_another_thread(tmp_path):
    # Streamlit caches the MetricStore via st.cache_resource, so the SAME object
    # (and its sqlite connection) is reused across script reruns, each of which
    # runs on a different thread. A connection created on one thread must be
    # usable from the others, or the dashboard crashes with
    # sqlite3.ProgrammingError. The check only fires once more than the owning
    # thread touches the connection, so we exercise it from several threads.
    db = tmp_path / "t.db"

    created_holder = {}

    def create():
        s = MetricStore(str(db))
        s.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
        created_holder["store"] = s

    t_create = threading.Thread(target=create)
    t_create.start()
    t_create.join()
    store = created_holder["store"]

    failures = []

    def use(tag):
        try:
            store.load_runner_profile()
        except Exception as e:  # noqa: BLE001
            failures.append(f"{tag}:{type(e).__name__}")

    threads = [threading.Thread(target=use, args=(f"run{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not failures, f"cross-thread use failed: {failures}"