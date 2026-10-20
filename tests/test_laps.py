"""Tests for the /splits parse layer (Task 5, criterion 7)."""

import json
from pathlib import Path

from normalize import parse_laps

FIXTURE = Path(__file__).parent / "fixtures" / "activity_splits.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_laps_maps_populated_lap():
    lap = parse_laps(_payload())[0]
    assert lap.index == 1
    assert lap.distance_m == 1000.0
    assert lap.duration_s == 368.653
    assert lap.avg_hr == 124.0
    assert lap.max_hr == 152.0
    assert lap.avg_cadence == 184.59
    assert lap.max_cadence == 192.0
    assert lap.elevation_gain_m == 4.2
    assert lap.avg_speed == 2.712
    assert lap.ground_contact_time == 260.2
    assert lap.stride_length_m == 85.52
    assert lap.vertical_oscillation_cm == 6.38
    assert lap.vertical_ratio_pct == 7.34
    assert lap.intensity == "WARMUP"


def test_parse_laps_missing_fields_yield_none():
    """Lap 2 in the fixture has no averageHR/averageRunCadence — every lap
    field is optional in the live payload; missing must become None, never
    raise."""
    lap = parse_laps(_payload())[1]
    assert lap.avg_hr is None
    assert lap.avg_cadence is None
    assert lap.stride_length_m is None
    assert lap.max_hr == 168.0
    assert lap.intensity is None


def test_parse_laps_keeps_intensity():
    lap = parse_laps(_payload())[2]
    assert lap.intensity == "INTERVAL"
    assert lap.index == 3


def test_parse_laps_empty_and_non_dict_payloads_yield_empty():
    for payload in (None, {}, {"activityId": 77}, {"lapDTOs": "nope"}, {"lapDTOs": {}}):
        assert parse_laps(payload or {}) == []


def test_parse_laps_ordered_even_without_lap_index():
    payload = {"lapDTOs": [{"distance": 400.0}, {"distance": 800.0}]}
    laps = parse_laps(payload)
    assert [lap.index for lap in laps] == [0, 1]


def test_parse_laps_never_raises_on_malformed_values():
    payload = {
        "lapDTOs": [
            {"distance": "warm", "averageHR": [], "lapIndex": None},
            "not a dict",
            {"duration": {"deep": "dict"}},
        ]
    }
    laps = parse_laps(payload)
    assert len(laps) == 2  # the string element is skipped
    assert laps[0].distance_m is None
    assert laps[0].duration_s is None  # dict value is not numeric → None
    assert laps[1].distance_m is None
