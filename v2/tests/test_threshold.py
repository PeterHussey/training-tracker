from datetime import date

import pandas as pd
import pytest

from metrics.threshold import parse_lt, approx_cs_1609
from normalize import Activity


LT_PAYLOAD = {
    "speed_and_heart_rate": {"heartRate": 168, "speed": 3.35,
                             "calendarDate": "2026-08-01", "sequence": 1, "userProfilePK": 1,
                             "version": None, "heartRateCycling": None},
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
    acts = [Activity(1, "running", d, 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {},
                     fastest_split_1609=320.1)]  # 1609m / 320.1s
    s = approx_cs_1609(acts)
    assert s.iloc[0] == pytest.approx(1609.0 / 320.1)