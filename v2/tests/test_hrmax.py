# tests/test_hrmax.py
from datetime import date

import pytest

from metrics.hrmax import estimate_hrmax
from normalize import Activity
from profile import RunnerProfile, default_profile, with_estimated_hrmax


def _act(day: int, max_hr, sport: str = "running") -> Activity:
    return Activity(
        activity_id=day, sport=sport, date=date(2026, 8, day),
        ts_ms=0, distance_m=0.0, duration_s=0.0, elapsed_s=0.0,
        avg_hr=None, max_hr=max_hr,
    )


def test_estimate_none_without_data():
    assert estimate_hrmax([]) is None


def test_estimate_none_without_recurrence():
    acts = [_act(1, 188.0), _act(2, 185.0)]
    assert estimate_hrmax(acts) is None


def test_estimate_uses_recurring_max_not_single_spike():
    acts = [_act(1, 181.0), _act(2, 181.0), _act(3, 188.0), _act(4, 180.0)]
    assert estimate_hrmax(acts) == 181


def test_estimate_ignores_values_below_floor():
    acts = [_act(1, 130.0), _act(2, 130.0), _act(3, 115.0), _act(4, 115.0)]
    assert estimate_hrmax(acts, floor=120) == 130


def test_estimate_none_when_all_below_floor():
    acts = [_act(1, 112.0), _act(2, 112.0)]
    assert estimate_hrmax(acts, floor=120) is None


def test_estimate_ignores_missing_max_hr():
    acts = [_act(1, None), _act(2, 160.0), _act(3, 160.0)]
    assert estimate_hrmax(acts) == 160


def test_estimate_requires_distinct_days_not_activities():
    # two activities on the SAME day must not count as "recurring"
    acts = [_act(1, 170.0), _act(1, 170.0), _act(2, 165.0), _act(3, 164.0)]
    assert estimate_hrmax(acts) is None


def test_with_estimated_hrmax_does_not_override_configured():
    p = default_profile(age=40, hrrest=60, sex="M")
    p = RunnerProfile(hrmax=185, hrrest=60, sex="M", birth_year=1986, hrmax_source="configured")
    out = with_estimated_hrmax(p, [_act(1, 181.0), _act(2, 181.0)])
    assert out.hrmax == 185
    assert out.hrmax_source == "configured"


def test_with_estimated_hrmax_sets_observed():
    p = default_profile(age=40, hrrest=60, sex="M")
    acts = [_act(1, 173.0), _act(2, 173.0), _act(3, 180.0)]
    out = with_estimated_hrmax(p, acts)
    assert out.hrmax == 173
    assert out.hrmax_source == "observed"
    assert out.hrrest == p.hrrest


def test_with_estimated_hrmax_keeps_age_predicted_without_estimate():
    p = default_profile(age=40, hrrest=60, sex="M")
    out = with_estimated_hrmax(p, [_act(1, 188.0), _act(2, 185.0)])
    assert out is p
    assert out.hrmax_source == "age_predicted"