# tests/test_backfill_names.py
from backfill_names import plan_name_updates


def _summary(aid, name):
    return {"activityId": aid, "activityName": name}


def test_plan_name_updates_fills_missing_and_changed_names():
    stored = {1: None, 2: "Lincoln - RF24 (Foundation Run)", 3: None, 4: "Treadmill Running"}
    raw = [
        _summary(1, "Winnetka - RF24 (Foundation Run)"),  # None -> name
        _summary(2, "Lincoln - RF24 (Foundation Run)"),  # unchanged
        _summary(3, ""),  # blank name in payload: kept as None, no update
        _summary(4, "Treadmill Running"),  # unchanged
        _summary(999, "Some Other Run"),  # unknown id: skipped
    ]
    assert plan_name_updates(stored, raw) == [(1, "Winnetka - RF24 (Foundation Run)")]


def test_plan_name_updates_updates_changed_name():
    stored = {1: "Old Name"}
    raw = [_summary(1, "New Name")]
    assert plan_name_updates(stored, raw) == [(1, "New Name")]


def test_plan_name_updates_empty_fetch_no_plans():
    assert plan_name_updates({1: None}, []) == []
