from datetime import date

import pytest

from metrics.gap import K, gap_pace
from normalize import Activity


def _run(d: date, dist_m: float, gain_m: float | None, speed_ms: float) -> Activity:
    return Activity(
        activity_id=int(d.strftime("%Y%m%d")),
        sport="running",
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=3600.0,
        elapsed_s=3600.0,
        avg_hr=None,
        max_hr=None,
        zone_s={},
        ele_gain_m=gain_m,
        ele_loss_m=0.0,
        avg_speed=speed_ms,
    )


def test_flat_run():
    acts = [_run(date(2026, 4, 1), 10_000.0, 0.0, 2.5)]
    gap = gap_pace(acts)
    assert gap.iloc[0] == pytest.approx(2.5)


def test_uphill_increases_gap():
    acts = [_run(date(2026, 4, 1), 10_000.0, 500.0, 2.5)]
    expected = 2.5 * (1.0 + K * 0.05)
    gap = gap_pace(acts)
    assert gap.iloc[0] == pytest.approx(expected)


def test_downhill_decreases_gap():
    acts = [_run(date(2026, 4, 1), 10_000.0, -500.0, 2.5)]
    expected = 2.5 * (1.0 + K * (-0.05))
    gap = gap_pace(acts)
    assert gap.iloc[0] == pytest.approx(expected)


def test_treadmill_excluded():
    acts = [_run(date(2026, 4, 1), 10_000.0, 0.0, 2.5)]
    acts[0] = Activity(
        activity_id=1,
        sport="treadmill",
        date=date(2026, 4, 1),
        ts_ms=0,
        distance_m=10_000.0,
        duration_s=3600.0,
        elapsed_s=3600.0,
        avg_hr=None,
        max_hr=None,
        zone_s={},
        ele_gain_m=None,
        avg_speed=2.5,
    )
    gap = gap_pace(acts)
    assert gap.empty


def test_no_elevation_excluded():
    acts = [_run(date(2026, 4, 1), 10_000.0, None, 2.5)]
    gap = gap_pace(acts)
    assert gap.empty


def test_no_speed_excluded():
    acts = [_run(date(2026, 4, 1), 10_000.0, 100.0, 0.0)]
    gap = gap_pace(acts)
    assert gap.empty


def test_multiple_runs_same_day_averaged():
    acts = [
        _run(date(2026, 4, 1), 10_000.0, 100.0, 2.5),
        _run(date(2026, 4, 1), 5_000.0, 50.0, 2.8),
    ]
    gap = gap_pace(acts)
    assert len(gap) == 1
    # grade1 = 0.01, gap1 = 2.5 * (1 + K*0.01)
    # grade2 = 0.01, gap2 = 2.8 * (1 + K*0.01)
    expected = (2.5 * (1 + K * 0.01) + 2.8 * (1 + K * 0.01)) / 2
    assert gap.iloc[0] == pytest.approx(expected)


def test_empty_input():
    gap = gap_pace([])
    assert gap.empty
