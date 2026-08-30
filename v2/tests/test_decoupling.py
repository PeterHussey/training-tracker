from datetime import date

import pytest

from metrics.decoupling import (
    aggregate_decoupling,
    decoupling_percent,
    eligible_activity,
    route_key,
)
from normalize import Activity


def _run(d, sport="running", dur_s=6000.0, dist_m=18000.0, gain_m=100.0, lat=None, lon=None):
    return Activity(activity_id=1, sport=sport, date=d, ts_ms=0, distance_m=dist_m,
                    duration_s=dur_s, elapsed_s=dur_s, avg_hr=150.0, max_hr=170.0,
                    zone_s={}, ele_gain_m=gain_m, lat=lat, lon=lon)


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
    assert out["n"] == 6 and out["mean"] == pytest.approx(sum([0.04, 0.05, 0.06, 0.07, 0.05, 0.06]) / 6)


def test_route_matching_picks_same_route_sessions():
    a = [_run(date(2026, 4, i), dur_s=6000.0, lat=42.4261, lon=-71.2801) for i in range(1, 9)]
    keys = {route_key(x) for x in a}
    assert len(keys) == 1
