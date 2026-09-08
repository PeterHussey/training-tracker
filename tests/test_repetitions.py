# tests/test_repetitions.py
"""Grouping activities into repeated-workout comparisons by their plan code."""

import json
from datetime import date
from pathlib import Path

from normalize import Activity, from_summary
from session import build_repetitions, repetition_rows

FIXTURES = Path(__file__).parent / "fixtures"


def _acts():
    raw = json.loads((FIXTURES / "activities_sample.json").read_text())
    return [from_summary(a) for a in raw]


def _act(activity_id, name, day):
    return Activity(
        activity_id=activity_id,
        sport="running",
        date=date(2026, 8, day),
        ts_ms=0,
        distance_m=5600.0,
        duration_s=2100.0,
        elapsed_s=2200.0,
        avg_hr=140.0,
        max_hr=160.0,
        zone_s={},
        ele_gain_m=20.0,
        vo2max=46.0,
        aerobic_te=3.0,
        anaerobic_te=0.5,
        avg_speed=2.66,
        name=name,
    )


def test_build_repetitions_groups_by_code():
    acts = [
        _act(1, "Winnetka - RF24 (Foundation Run)", 3),
        _act(2, "Lincoln - RF24 (Foundation Run)", 10),
        _act(3, "Lincoln - RHR22 (Hill Repetitions Run)", 5),
        _act(4, "Treadmill Running", 7),
    ]
    reps = build_repetitions(acts)
    assert set(reps) == {"RF24", "RHR22"}
    assert [a.activity_id for a in reps["RF24"]] == [1, 2]
    assert [a.activity_id for a in reps["RHR22"]] == [3]


def test_build_repetitions_omits_unlabeled():
    acts = [_act(1, "Treadmill Running", 7), _act(2, "Strength", 8)]
    assert build_repetitions(acts) == {}


def test_repetition_rows_are_chronological():
    acts = [
        _act(11, "Winnetka - RF24 (Foundation Run)", 19),
        _act(9, "Lincoln - RF24 (Foundation Run)", 15),
    ]
    rows = repetition_rows(acts, "RF24")
    assert [r["date"] for r in rows] == ["2026-08-15", "2026-08-19"]
    assert rows[0]["activity_id"] == 9
    assert rows[1]["activity_id"] == 11


def test_repetition_rows_fields():
    acts = [_act(9, "Lincoln - RF24 (Foundation Run)", 15)]
    row = repetition_rows(acts, "RF24")[0]
    assert row["activity_id"] == 9
    assert row["name"] == "Lincoln - RF24 (Foundation Run)"
    assert row["avg_hr"] == 140.0
    assert row["max_hr"] == 160.0
    assert row["distance_km"] == round(5600.0 / 1000.0, 6)
    assert row["aerobic_te"] == 3.0
    assert row["avg_speed"] == 2.66


def test_repetition_rows_empty_for_missing_code():
    acts = [_act(9, "Lincoln - RF24 (Foundation Run)", 15)]
    assert repetition_rows(acts, "RHR22") == []
