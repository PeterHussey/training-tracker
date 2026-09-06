# tests/test_racepredict.py
import json
from pathlib import Path

from metrics.racepredict import DISTANCE_KEYS, daily_predictions_from_trend, parse_predictions

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


def test_live_schema_flat_seconds():
    payload = {
        "time5K": 1405,
        "time10K": 3076,
        "timeHalfMarathon": 7420,
        "timeMarathon": 17179,
        "calendarDate": "2026-08-28",
    }
    out = parse_predictions(payload)
    assert out == {"5k": 1405, "10k": 3076, "half": 7420, "full": 17179}


def test_daily_trend_builds_per_distance_series():
    trend = [
        {"calendarDate": "2026-08-28", "time5K": 1405, "time10K": 3076},
        {
            "calendarDate": "2026-09-03",
            "time5K": 1413,
            "time10K": 3100,
            "timeHalfMarathon": 7460,
            "timeMarathon": 17267,
        },
    ]
    out = daily_predictions_from_trend(trend)
    assert set(out) == {"5k", "10k", "half", "full"}
    assert out["5k"]["2026-08-28"] == 1405
    assert out["5k"]["2026-09-03"] == 1413
    # missing keys on a day stay missing (no forward-fill across days)
    assert len(out["half"]) == 1
    assert out["full"]["2026-09-03"] == 17267


def test_daily_trend_empty_returns_empty_series():
    out = daily_predictions_from_trend([])
    assert set(out) == {"5k", "10k", "half", "full"}
    assert all(len(s) == 0 for s in out.values())


def test_daily_trend_duplicate_dates_keep_last():
    trend = [
        {"calendarDate": "2026-08-28", "time5K": 1405},
        {"calendarDate": "2026-08-28", "time5K": 1413},
    ]
    out = daily_predictions_from_trend(trend)
    assert out["5k"]["2026-08-28"] == 1413
