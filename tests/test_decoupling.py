from datetime import date

import pytest

from metrics.decoupling import (
    aggregate_decoupling,
    decoupling_percent,
    decoupling_timebased,
    eligible_activity,
    route_key,
)
from normalize import Activity


def _run(d, sport="running", dur_s=6000.0, dist_m=18000.0, gain_m=100.0, lat=None, lon=None):
    return Activity(
        activity_id=1,
        sport=sport,
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=dur_s,
        elapsed_s=dur_s,
        avg_hr=150.0,
        max_hr=170.0,
        zone_s={},
        ele_gain_m=gain_m,
        lat=lat,
        lon=lon,
    )


def test_no_drift_is_zero_percent():
    hr = [150.0] * 100
    speed = [3.0] * 100
    assert decoupling_percent(hr, speed) == pytest.approx(0.0)


def test_hr_drift_shows_positive_decoupling():
    hr = [145.0] * 50 + [155.0] * 50
    speed = [3.0] * 100
    dec = decoupling_percent(hr, speed)
    assert dec > 0.05


def test_eligibility_min_duration():
    short = _run(date(2026, 4, 1), dur_s=1800.0)
    assert not eligible_activity(short)
    long = _run(date(2026, 4, 2), dur_s=7200.0)
    assert eligible_activity(long)


def test_eligibility_rejects_hills_and_non_running():
    hilly = _run(date(2026, 4, 3), dist_m=10_000.0, gain_m=800.0)  # 80 m/km > 25
    assert not eligible_activity(hilly)
    assert not eligible_activity(_run(date(2026, 4, 4), sport="treadmill", gain_m=None))


def test_route_key_clusters():
    a1 = _run(date(2026, 4, 1), lat=42.4261, lon=-71.2801)
    a2 = _run(date(2026, 4, 2), lat=42.4265, lon=-71.2805)
    a3 = _run(date(2026, 4, 3))  # no coords
    assert route_key(a1) == route_key(a2)
    assert route_key(a3) == ("unknown", None)


def test_aggregate_requires_six_sessions():
    assert aggregate_decoupling([0.04, 0.05, 0.06], min_sessions=6) is None
    out = aggregate_decoupling([0.04, 0.05, 0.06, 0.07, 0.05, 0.06], min_sessions=6)
    assert out["n"] == 6 and out["mean"] == pytest.approx(
        sum([0.04, 0.05, 0.06, 0.07, 0.05, 0.06]) / 6
    )


def test_route_matching_picks_same_route_sessions():
    a = [_run(date(2026, 4, i), dur_s=6000.0, lat=42.4261, lon=-71.2801) for i in range(1, 9)]
    keys = {route_key(x) for x in a}
    assert len(keys) == 1


def test_eligibility_excludes_structured_intervals():
    a = _run(date(2026, 4, 5), dur_s=7200.0)
    a.has_intervals = True
    assert not eligible_activity(a)
    assert eligible_activity(a, exclude_intervals=False)


def test_timebased_split_uses_elapsed_time_not_sample_count():
    # 100-min effort, HR drifts 145 -> 155 at the elapsed-time midpoint.
    # Dense sampling early (1 Hz), sparse late (0.1 Hz): a sample-count
    # split puts mostly early samples in both halves (~1% drift) while the
    # time-based split sees the real ~6.9% drift.
    hr, speed, ts = [], [], []
    t = 0
    for _ in range(3000):
        hr.append(145.0)
        speed.append(3.0)
        ts.append(t)
        t += 1000
    for _ in range(300):
        hr.append(155.0)
        speed.append(3.0)
        ts.append(t)
        t += 10000
    assert decoupling_timebased(hr, speed, ts) == pytest.approx(155.0 / 145.0 - 1.0, rel=0.05)
    assert decoupling_percent(hr, speed) < 0.02


def test_timebased_drops_dropouts_and_rejects_empty():
    hr = [145.0, None, 155.0, 156.0]
    speed = [3.0, 3.0, None, 3.1]
    ts = [0, 60_000, 120_000, 180_000]
    dec = decoupling_timebased(hr, speed, ts)
    assert dec == pytest.approx((156.0 / 3.1) / (145.0 / 3.0) - 1.0)
    with pytest.raises(ValueError):
        decoupling_timebased([150.0, None], [3.0, None], [0, 1000])
