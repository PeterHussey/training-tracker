from datetime import date
from profile import default_profile

import pytest

from metrics.threshold import (
    approx_cs_1609,
    best_effort_anchors,
    best_effort_lthr,
    best_window,
    parse_details_series,
    parse_lt,
    rolling_lt_hr,
    rolling_lt_pace,
)
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
    return Activity(1, sport, d, 0, 5000.0, dur_s, dur_s, hr, 170.0, {}, fastest_split_1609=split)


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
        Activity(
            1, "running", d, 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {}, fastest_split_1609=320.1
        ),
        Activity(
            2,
            "running",
            date(2026, 4, 3),
            0,
            5000.0,
            1800.0,
            1800.0,
            150.0,
            170.0,
            {},
            fastest_split_1609=310.0,
        ),
    ]
    s = rolling_lt_pace(acts)
    assert len(s) == 2
    # Most recent CS in each 45-day window
    assert s.iloc[0] == pytest.approx(1609.0 / 320.1)
    assert s.iloc[1] == pytest.approx(1609.0 / 310.0)


def test_rolling_lt_pace_includes_treadmill():
    acts = [
        Activity(
            1,
            "running",
            date(2026, 4, 1),
            0,
            5000.0,
            1800.0,
            1800.0,
            150.0,
            170.0,
            {},
            fastest_split_1609=320.1,
        ),
        Activity(
            2,
            "treadmill",
            date(2026, 4, 3),
            0,
            5000.0,
            1800.0,
            1800.0,
            150.0,
            170.0,
            {},
            fastest_split_1609=310.0,
        ),
    ]
    s = rolling_lt_pace(acts)
    assert len(s) == 2
    assert s.iloc[1] == pytest.approx(1609.0 / 310.0)


# --- details-series parser + best-window ---


def test_parse_details_series_extracts_hr_and_speed():
    details = {
        "metrics": [
            {"heartRate": 150.0, "speed": 2.8, "distance": 0.0},
            {"heartRate": None, "speed": 3.0, "distance": 3.0},
            {"heartRate": 172.0, "speed": 3.8, "distance": 6.8},
        ]
    }
    hr, speed = parse_details_series(details)
    assert hr == [150.0, None, 172.0]
    assert speed == [2.8, 3.0, 3.8]


def test_best_window_picks_fastest_contiguous_segment():
    hr = [150.0] * 600 + [172.0] * 1200 + [150.0] * 600
    speed = [2.8] * 600 + [3.8] * 1200 + [2.8] * 600
    w = best_window(hr, speed, window_s=1200)
    assert w["mean_speed"] == pytest.approx(3.8)
    assert w["mean_hr"] == pytest.approx(172.0)


def test_best_window_gap_breaks_contiguity():
    hr = [172.0] * 1200
    speed = [3.8] * 1200
    hr[600] = None  # optical gap > max_gap_s at 1Hz
    w = best_window(hr, speed, window_s=1200, max_gap_s=0.5)
    assert w is None


def test_best_window_gap_within_tolerance_accepted():
    """A 3-sample gap (3 s) with max_gap_s=5.0 should be accepted."""
    hr: list[float | None] = [172.0] * 1200
    speed: list[float | None] = [3.8] * 1200
    hr[599] = None
    hr[600] = None
    hr[601] = None  # 3 s gap ≤ 5 s tolerance
    w = best_window(hr, speed, window_s=1200, max_gap_s=5.0)
    assert w is not None
    assert w["mean_speed"] == pytest.approx(3.8)
    assert w["mean_hr"] == pytest.approx(172.0)


def test_best_window_gap_exceeding_tolerance_rejected():
    """A 6-sample gap (6 s) with max_gap_s=5.0 should still return None."""
    hr: list[float | None] = [172.0] * 1200
    speed: list[float | None] = [3.8] * 1200
    hr[597] = None
    hr[598] = None
    hr[599] = None
    hr[600] = None
    hr[601] = None
    hr[602] = None  # 6 s gap > 5 s tolerance
    w = best_window(hr, speed, window_s=1200, max_gap_s=5.0)
    assert w is None


# --- best-effort cross-activity anchor ---


def _make_series(n: int, hr_val: float, speed_val: float) -> tuple[list[float], list[float]]:
    """Helper: create uniform hr/speed lists of length n."""
    return [hr_val] * n, [speed_val] * n


def test_best_effort_outdoor_only_and_factor():
    """Treadmill + cross + short + hr-less candidates excluded; fastest outdoor 20-min wins; proxy = raw * 0.95."""
    d1, d2, d3, d4, d5 = (date(2026, 4, i) for i in range(1, 6))

    # Outdoor running, 20 min, HR 165, speed 3.5 — the winner
    a1 = Activity(10, "running", d1, 0, 5000.0, 1200.0, 1200.0, 165.0, 180.0, {})
    # Outdoor running, 20 min, HR 158, speed 3.2 — slower
    a2 = Activity(20, "running", d2, 0, 5000.0, 1200.0, 1200.0, 158.0, 175.0, {})
    # Treadmill — excluded
    a3 = Activity(30, "treadmill", d3, 0, 5000.0, 1200.0, 1200.0, 162.0, 178.0, {})
    # Cross-training — excluded
    a4 = Activity(40, "cross", d4, 0, 5000.0, 1200.0, 1200.0, 160.0, 176.0, {})
    # Outdoor running, but only 10 min — too short for 20-min window
    a5 = Activity(50, "running", d5, 0, 5000.0, 600.0, 600.0, 170.0, 185.0, {})
    # Outdoor running, 20 min, but no HR — excluded
    a6 = Activity(60, "running", date(2026, 4, 6), 0, 5000.0, 1200.0, 1200.0, None, None, {})

    activities = [a1, a2, a3, a4, a5, a6]

    # Series: only for the outdoor running activities that qualify by duration+HR
    series_by_id = {
        10: _make_series(1200, 165.0, 3.5),  # winner
        20: _make_series(1200, 158.0, 3.2),
        30: _make_series(1200, 162.0, 3.4),  # treadmill — ignored
        40: _make_series(1200, 160.0, 3.3),  # cross — ignored
        # 50: no series (doesn't matter, too short)
        # 60: no series (doesn't matter, no HR)
    }

    anchor = best_effort_lthr(activities, series_by_id, window_s=1200, factor=0.95)
    assert anchor is not None
    assert anchor["raw_hr"] == pytest.approx(165.0)
    assert anchor["proxy_hr"] == pytest.approx(165.0 * 0.95)
    assert anchor["pace"] == pytest.approx(1.0 / 3.5)  # pace = 1/speed
    assert anchor["activity_id"] == 10
    assert anchor["window_s"] == 1200
    assert anchor["factor"] == pytest.approx(0.95)


def test_best_effort_30min_factor():
    """30-min window uses factor 0.97."""
    d1 = date(2026, 5, 1)
    a1 = Activity(1, "running", d1, 0, 10000.0, 1800.0, 1800.0, 160.0, 180.0, {})
    a2 = Activity(2, "running", date(2026, 5, 2), 0, 10000.0, 1800.0, 1800.0, 155.0, 178.0, {})

    series_by_id = {
        1: _make_series(1800, 160.0, 3.0),
        2: _make_series(1800, 155.0, 3.2),  # faster but lower HR
    }

    anchor = best_effort_lthr(
        activities=[a1, a2], series_by_id=series_by_id, window_s=1800, factor=0.97
    )
    assert anchor is not None
    assert anchor["activity_id"] == 2  # higher speed wins
    assert anchor["raw_hr"] == pytest.approx(155.0)
    assert anchor["proxy_hr"] == pytest.approx(155.0 * 0.97)


def test_best_effort_no_qualifying_returns_none():
    """No qualifying activities returns None."""
    a1 = Activity(1, "treadmill", date(2026, 1, 1), 0, 5000.0, 1200.0, 1200.0, 160.0, 175.0, {})
    anchor = best_effort_lthr([a1], {1: _make_series(1200, 160.0, 3.0)}, window_s=1200, factor=0.95)
    assert anchor is None


def test_best_effort_anchors_dots():
    """best_effort_anchors returns dots for every qualifying activity."""
    acts = [
        Activity(1, "running", date(2026, 6, 1), 0, 5000.0, 1200.0, 1200.0, 165.0, 180.0, {}),
        Activity(2, "running", date(2026, 6, 2), 0, 5000.0, 1200.0, 1200.0, 158.0, 175.0, {}),
        Activity(3, "running", date(2026, 6, 3), 0, 8000.0, 1800.0, 1800.0, 160.0, 178.0, {}),
        Activity(4, "treadmill", date(2026, 6, 4), 0, 5000.0, 1200.0, 1200.0, 162.0, 176.0, {}),
    ]
    series = {
        1: _make_series(1200, 165.0, 3.5),
        2: _make_series(1200, 158.0, 3.2),
        3: _make_series(1800, 160.0, 2.9),
        4: _make_series(1200, 162.0, 3.4),
    }

    result = best_effort_anchors(acts, series)

    # 20-min: three qualifying outdoor activities (IDs 1, 2, 3 — activity 3 is 30 min so also qualifies)
    assert result["w20"] is not None
    assert result["w20"]["activity_id"] == 1
    assert len(result["dots20"]) == 3
    dot_ids_20 = {d["activity_id"] for d in result["dots20"]}
    assert dot_ids_20 == {1, 2, 3}

    # 30-min: one qualifying outdoor activity (ID 3)
    assert result["w30"] is not None
    assert result["w30"]["activity_id"] == 3
    assert len(result["dots30"]) == 1
    assert result["dots30"][0]["activity_id"] == 3

    # Each dot has the required keys
    for dot in result["dots20"] + result["dots30"]:
        assert set(dot.keys()) == {"hr", "pace", "date", "activity_id"}
