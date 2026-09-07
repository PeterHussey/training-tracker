from datetime import date

import pytest

from metrics.threshold import (
    approx_cs_1609,
    best_effort_anchors,
    best_effort_lthr,
    best_window,
    parse_details_series,
    parse_lt,
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


# --- details-series parser + best-window ---


def _live_shape_details(rows: list[tuple]) -> dict:
    """Build a live-shape details payload: descriptors + column-indexed samples.

    rows: list of (timestamp_ms, speed_m_s, hr_bpm); None allowed for hr/speed.
    """
    return {
        "metricDescriptors": [
            {"metricsIndex": 0, "key": "directTimestamp"},
            {"metricsIndex": 1, "key": "directSpeed"},
            {"metricsIndex": 2, "key": "directHeartRate"},
        ],
        "activityDetailMetrics": [{"metrics": [ts, spd, hr]} for ts, spd, hr in rows],
    }


def test_parse_details_series_extracts_hr_speed_timestamps():
    details = _live_shape_details(
        [
            (1_700_000_000_000.0, 2.8, 150.0),
            (1_700_000_001_000.0, 3.0, None),
            (1_700_000_008_000.0, 3.8, 172.0),
        ]
    )
    hr, speed, ts = parse_details_series(details)
    assert hr == [150.0, None, 172.0]
    assert speed == [2.8, 3.0, 3.8]
    assert ts == [1_700_000_000_000.0, 1_700_000_001_000.0, 1_700_000_008_000.0]


def test_parse_details_series_missing_descriptor_returns_empty():
    details = {
        "metricDescriptors": [{"metricsIndex": 0, "key": "directSpeed"}],
        "activityDetailMetrics": [{"metrics": [3.0]}],
    }
    assert parse_details_series(details) == ([], [], [])


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


# --- time-domain best_window (irregular sampling) ---


def _irregular_series(
    segments: list[tuple[int, float, float]], step_ms: float = 4000.0, t0: float = 0.0
) -> tuple[list[float], list[float], list[float]]:
    """Build (hr, speed, ts_ms) with one sample per step_ms per segment.

    segments: list of (n_samples, hr_val, speed_val).
    """
    hr: list[float] = []
    speed: list[float] = []
    ts: list[float] = []
    t = t0
    for n, h, s in segments:
        for _ in range(n):
            hr.append(h)
            speed.append(s)
            ts.append(t)
            t += step_ms
    return hr, speed, ts


def test_best_window_time_domain_picks_fastest_span():
    # 300 samples @ 4 s = 1200 s slow, then 300 @ 4 s fast, then 300 slow.
    # The winning window starts at the fast segment; it also catches the
    # boundary slow sample at exactly +1200 s (inclusive end).
    hr, speed, ts = _irregular_series([(300, 150.0, 2.8), (300, 172.0, 3.8), (300, 150.0, 2.8)])
    w = best_window(hr, speed, window_s=1200, ts_ms=ts)
    assert w is not None
    # Windows starting at 299 and 300 tie (1 slow + 300 fast either way);
    # first strictly-greater wins, so 299.
    assert w["start_idx"] in (299, 300)
    assert w["mean_speed"] == pytest.approx((300 * 3.8 + 2.8) / 301)
    assert w["mean_hr"] == pytest.approx((300 * 172.0 + 150.0) / 301)


def test_best_window_time_domain_gap_rejects():
    # A 60 s hole in the middle of an otherwise fast 1200 s span.
    hr, speed, ts = _irregular_series([(150, 172.0, 3.8), (150, 172.0, 3.8)])
    # Punch a 60 s gap: shift the second half forward.
    ts = ts[:150] + [t + 60_000.0 for t in ts[150:]]
    w = best_window(hr, speed, window_s=1200, ts_ms=ts, max_gap_s=5.0)
    assert w is None


def test_best_window_time_domain_short_span_returns_none():
    # Only 100 s of data can never fill a 1200 s window.
    hr, speed, ts = _irregular_series([(25, 172.0, 3.8)])
    assert best_window(hr, speed, window_s=1200, ts_ms=ts) is None


def test_best_window_time_domain_sparse_sampling_accepted():
    # Live data arrives ~9 s apart with no dropouts; the gap tolerance
    # adapts to the sampling rate instead of rejecting every window.
    hr, speed, ts = _irregular_series([(140, 172.0, 3.8)], step_ms=9000.0)  # 140 x 9 s = 1260 s
    w = best_window(hr, speed, window_s=1200, ts_ms=ts, max_gap_s=5.0)
    assert w is not None
    assert w["mean_speed"] == pytest.approx(3.8)
    assert w["mean_hr"] == pytest.approx(172.0)


# --- best-effort cross-activity anchor ---


def _make_series(
    n: int, hr_val: float, speed_val: float
) -> tuple[list[float], list[float], list[float]]:
    """Helper: create uniform (hr, speed, ts_ms) 1 Hz series of length n."""
    return [hr_val] * n, [speed_val] * n, [float(i * 1000) for i in range(n)]


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
