# tests/test_racepredict.py
import json
from pathlib import Path

import pytest

from metrics.racepredict import DISTANCE_KEYS, parse_predictions

FIXTURE = Path(__file__).parent / "fixtures" / "race_predictions.json"


def test_canonical_schema_parses_ms_to_seconds():
    payload = {
        "Run_5k": {"time": 1500000},
        "Run_10k": {"time": 3100000},
        "Run_half_marathon": {"time": 6800000},
        "Run_full_marathon": {"time": 14400000},
    }
    out = parse_predictions(payload)
    assert out["5k"] == 1500
    assert out["full"] == 14400


def test_missing_events_become_none():
    out = parse_predictions({"Run_5k": {"time": 1500000}})
    assert out["10k"] is None


def test_frozen_payload_parses():
    data = json.loads(FIXTURE.read_text())
    out = parse_predictions(data)
    for k in DISTANCE_KEYS:
        assert k in out
