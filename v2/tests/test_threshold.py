from datetime import date
from profile import default_profile

import pytest

from metrics.threshold import approx_cs_1609, parse_lt, rolling_lt_hr, rolling_lt_pace
from normalize import Activity

LT_PAYLOAD = {
    "speed_and_heart_rate": {
        "heartRate": 168,
        "speed": 3.35,
        "calendarDate": "2026-08-01",
        "sequence": 1,
        "userProfilePK": 1,
        "version": None,
        "heartRateCycling": None,
    },
    "power": {},
}


def test_parse_lt_maps_hr_speed_date():
    lt = parse_lt(LT_PAYLOAD)
    assert lt.hr == 168
    assert lt.speed_m_s == pytest.approx(3.35)
    assert lt.date == "2026-08-01"


def test_parse_lt_tolerates_missing_power():
    lt = parse_lt({"speed_and_heart_rate": {}, "power": None})
    assert lt.hr is None and lt.speed_m_s is None


def test_approx_cs_from_fastest_mile():
    d = date(2026, 4, 1)
    acts = [
        Activity(
            1, "running", d, 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=320.1
        )
    ]  # 1609m / 320.1s
    s = approx_cs_1609(acts)
    assert s.iloc[0] == pytest.approx(1609.0 / 320.1)


def _act_hr(d: date, sport: str, dur_s: float, hr: float, split: float = None) -> Activity:
    return Activity(
        1, sport, d, 0, 5000.0, dur_s, dur_s, hr, 170.0, {}, fastest_split_1609=split
    )


def test_rolling_lt_hr_requires_sustained_efforts():
    p = default_profile(age=40, hrrest=60, sex="M")  # hrmax=180, threshold=153 bpm
    acts = [
        _act_hr(date(2026, 4, 1), "running", 1800.0, 140.0),  # 30 min, 140 < 153 (not sustained)
        _act_hr(date(2026, 4, 3), "running", 1200.0, 160.0),  # 20 min, 160 >= 153 (sustained)
        _act_hr(date(2026, 4, 5), "running", 1500.0, 165.0),  # 25 min, 165 >= 153 (sustained)
    ]
    s = rolling_lt_hr(acts, p)
    assert len(s) == 1  # only 2026-04-05 has 2 sustained efforts in its 30-day window
    assert s.iloc[0] == pytest.approx((160.0 + 165.0) / 2)


def test_rolling_lt_hr_includes_treadmill_and_cross():
    p = default_profile(age=40, hrrest=60, sex="M")  # hrmax=180, threshold=153 bpm
    acts = [
        _act_hr(date(2026, 4, 1), "running", 1200.0, 160.0),
        _act_hr(date(2026, 4, 3), "treadmill", 1200.0, 165.0),
        _act_hr(date(2026, 4, 5), "cross", 1200.0, 170.0),
    ]
    s = rolling_lt_hr(acts, p)
    assert len(s) == 2  # 2026-04-03 has 2, 2026-04-05 has 3 sustained efforts
    assert s.iloc[0] == pytest.approx((160.0 + 165.0) / 2)
    assert s.iloc[1] == pytest.approx((160.0 + 165.0 + 170.0) / 3)


def test_rolling_lt_pace_from_critical_speed():
    d = date(2026, 4, 1)
    acts = [
        Activity(1, "running", d, 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=320.1),
        Activity(2, "running", date(2026, 4, 3), 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=310.0),
    ]
    s = rolling_lt_pace(acts)
    assert len(s) == 2
    # Most recent CS in each 45-day window
    assert s.iloc[0] == pytest.approx(1609.0 / 320.1)
    assert s.iloc[1] == pytest.approx(1609.0 / 310.0)


def test_rolling_lt_pace_includes_treadmill():
    acts = [
        Activity(1, "running", date(2026, 4, 1), 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=320.1),
        Activity(2, "treadmill", date(2026, 4, 3), 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=310.0),
    ]
    s = rolling_lt_pace(acts)
    assert len(s) == 2
    assert s.iloc[1] == pytest.approx(1609.0 / 310.0)
