# tests/test_store.py
import json
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