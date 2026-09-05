# tests/test_normalize.py
import json
from datetime import date
from pathlib import Path

from normalize import from_summary, sport_of

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"
DATA = json.loads(FIXTURE.read_text())


def _first(key):
    return next(a for a in DATA if a.get("activityType", {}).get("typeKey") == key)


def test_sport_classification():
    assert sport_of(_first("running")) == "running"
    assert sport_of(_first("treadmill_running")) == "treadmill"
    assert sport_of(_first("indoor_cycling")) == "cross"
    assert sport_of(_first("strength_training")) == "strength"


def test_from_summary_maps_core_fields():
    a = from_summary(_first("running"))
    assert a.sport == "running"
    assert isinstance(a.date, date)
    assert a.distance_m > 0
    assert a.duration_s > 0
    assert a.avg_hr is None or a.avg_hr > 0
    assert set(a.zone_s) == {1, 2, 3, 4, 5}
    assert a.distance_km == round(a.distance_m / 1000.0, 6)
    assert a.duration_min == round(a.duration_s / 60.0, 6)


def test_missing_optional_fields_become_none():
    a = from_summary(_first("indoor_cycling"))
    assert a.ele_gain_m is None
    assert a.vo2max is None
    assert a.fastest_split_1609 is None


def test_zone_seconds_default_zero():
    a = from_summary(_first("strength_training"))
    assert a.zone_s[1] >= 0 and a.zone_s[5] >= 0
