"""WMA 2023 age grading: table lookups, clamping, log-log interpolation, grade().

Pinned values verified against the official WMA 2023 Age Factors PDF
(world-masters-athletics.org, effective 1/1/2023) and ratified open WRs.
"""

from datetime import date
from profile import RunnerProfile

import pytest

from metrics.age_grading import daily_best_graded, factor, grade
from normalize import Activity

# --- factor(): exact table lookups (WMA 2023 one-year factors) ---


def test_factor_exact_anchors_male():
    assert factor(40, "M", "marathon") == pytest.approx(0.9804)
    assert factor(60, "M", "marathon") == pytest.approx(0.8331)
    assert factor(71, "M", "half_marathon") == pytest.approx(0.7534)


def test_factor_exact_anchors_female():
    assert factor(40, "F", "5000m") == pytest.approx(0.9615)
    assert factor(45, "F", "mile") == pytest.approx(0.9085)
    assert factor(35, "F", "marathon") == pytest.approx(0.9982)


def test_factor_age_30_is_open_class():
    assert factor(30, "M", "5000m") == pytest.approx(1.0)
    assert factor(30, "F", "10000m") == pytest.approx(1.0)


def test_factor_clamps_age_below_table():
    assert factor(20, "M", "marathon") == factor(30, "M", "marathon")


def test_factor_clamps_age_above_table():
    assert factor(115, "F", "5000m") == factor(110, "F", "5000m")


# --- grade(): distance range gates ---


def test_grade_returns_none_below_3000m():
    assert grade(2999.9, 600.0, 40, "M") is None


def test_grade_returns_none_above_marathon():
    assert grade(42195.1, 15000.0, 40, "M") is None


def test_grade_accepts_range_boundaries():
    assert grade(3000.0, 600.0, 40, "M") is not None
    assert grade(42195.0, 15000.0, 40, "M") is not None


# --- grade(): exact anchor distances ---


def test_grade_male_5000m_age40():
    # F = 0.9701, WR = 755.36; t = 1000 -> graded 970.1, pct 755.36/970.1
    pct, graded = grade(5000.0, 1000.0, 40, "M")
    assert graded == pytest.approx(970.1, rel=1e-4)
    assert pct == pytest.approx(0.77864, rel=1e-4)


def test_grade_female_10000m_age60():
    # F = 0.7961, WR = 1734.14; t = 2000 -> graded 1592.2, pct 1734.14/1592.2
    pct, graded = grade(10000.0, 2000.0, 60, "F")
    assert graded == pytest.approx(1592.2, rel=1e-4)
    assert pct == pytest.approx(1.08915, rel=1e-4)


def test_grade_world_record_at_age_30_is_100pct():
    pct, graded = grade(42195.0, 7170.0, 30, "M")
    assert graded == pytest.approx(7170.0, rel=1e-6)
    assert pct == pytest.approx(1.0, rel=1e-6)


# --- grade(): interpolation between anchors (log-log) ---


def test_grade_interpolates_7000m():
    # Between 5000m (WR 755.36, F 0.9701) and 10000m (WR 1571.0, F 0.9523).
    # Log-log at 7000m: WR ~= 1078.0, F ~= 0.96142; t = 1400.
    pct, graded = grade(7000.0, 1400.0, 40, "M")
    assert graded == pytest.approx(1346.0, rel=1e-3)
    assert pct == pytest.approx(0.8009, rel=1e-3)


def test_grade_interpolation_monotonic_in_distance():
    # Same pace (5 m/s) at increasing distance -> graded time increases.
    graded = [grade(d, d / 5.0, 40, "M")[1] for d in (3000.0, 5000.0, 7000.0, 10000.0)]
    assert graded == sorted(graded)


def test_grade_uses_clamped_age():
    assert grade(5000.0, 1000.0, 20, "M") == grade(5000.0, 1000.0, 30, "M")


# --- daily_best_graded(): per-day best pct over eligible outdoor runs ---

PROFILE_40M = RunnerProfile(hrmax=180, hrrest=60, sex="M", birth_year=1986, units="metric")


def _act(aid: int, sport: str, d: date, dist_m: float, dur_s: float) -> Activity:
    return Activity(
        activity_id=aid,
        sport=sport,
        date=d,
        ts_ms=0,
        distance_m=dist_m,
        duration_s=dur_s,
        elapsed_s=dur_s,
        avg_hr=None,
        max_hr=None,
    )


def test_daily_best_graded_picks_best_per_day():
    acts = [
        # 2026-01-10, age 40: 5000m pct 0.77864 vs 10000m pct 0.82485 -> best wins
        _act(1, "running", date(2026, 1, 10), 5000.0, 1000.0),
        _act(2, "running", date(2026, 1, 10), 10000.0, 2000.0),
        _act(3, "running", date(2026, 1, 11), 5000.0, 1000.0),
    ]
    s = daily_best_graded(acts, PROFILE_40M)
    assert len(s) == 2
    assert s.loc[s.index[0]] == pytest.approx(0.82485, rel=1e-4)
    assert s.loc[s.index[1]] == pytest.approx(0.77864, rel=1e-4)
    assert s.index.is_monotonic_increasing


def test_daily_best_graded_excludes_treadmill_and_short_runs():
    acts = [
        _act(1, "treadmill", date(2026, 1, 10), 5000.0, 900.0),
        _act(2, "running", date(2026, 1, 10), 2500.0, 500.0),
        _act(3, "cross", date(2026, 1, 10), 5000.0, 900.0),
    ]
    assert daily_best_graded(acts, PROFILE_40M).empty


def test_daily_best_graded_empty_activities():
    assert daily_best_graded([], PROFILE_40M).empty
