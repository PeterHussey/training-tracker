"""Golden tests for the ``BasicEndurance`` / ``MarathonShape`` port.

Numbers come from the upstream ``BasicEnduranceTest`` and ``MarathonShapeTest``.
Both are MySQL/SQLite-dependent (they insert ``training`` rows and read them back
through the legacy ``DB::`` facade), so only their assertion values are ported
here. Run tuples are ``(days_ago, distance_km)`` relative to the as-of moment,
which is how the PHP's ``(timestamp - time) / DAY_IN_S`` reads after the window
boundaries have been applied.
"""

import pytest

from metrics import marathon_shape

# --- BasicEnduranceTest::testTargets ---------------------------------------


@pytest.mark.parametrize(
    "vdot, week_env, lj_real",
    [
        (30, 47.5, 24),
        (40, 66.0, 27.5),
        (50, 85.0, 30.0),
        (60, 104.0, 32.5),
        (70, 124.0, 34.5),
        (80, 145.0, 36.0),
    ],
)
def test_targets_match_upstream(vdot, week_env, lj_real):
    assert marathon_shape.target_week_km(vdot) == pytest.approx(week_env, abs=1.0)
    assert marathon_shape.target_lj_km_week_real(vdot) == pytest.approx(lj_real, abs=1.0)


# --- MarathonShapeTest::testThatVerySmallVO2maxValuesAreIgnored -------------


@pytest.mark.parametrize("vdot", [1.0, 20.0])
def test_soft_peaks_to_unrealistic_inputs(vdot):
    # Both clamp to VO2MAX floor 25: 25**1.135 = 38.6, ln(25/4)*12 = 21.99.
    assert marathon_shape.target_week_km(vdot) == pytest.approx(37.99, abs=1.0)
    assert marathon_shape.target_lj_km_week_real(vdot) == pytest.approx(21.99, abs=0.1)


def test_reduced_target_subtracts_the_longjog_minimum():
    # getTargetLongjogKmPerWeek = real target - MIN_KM_FOR_LONGJOG (13 default).
    real = marathon_shape.target_lj_km_week_real(30.0)
    assert real == pytest.approx(24.179, abs=0.001)
    assert marathon_shape.target_lj_km_week_reduced(30.0) == pytest.approx(11.179, abs=0.001)
    assert marathon_shape.target_lj_km_week_reduced(30.0) == pytest.approx(real - 13.0)


# --- Windows and totals -----------------------------------------------------


def test_week_km_window_is_strict():
    # time > timestamp - days*DAY_IN_S: a run exactly `days` ago falls out.
    runs = [(6.9, 10.0), (7.0, 20.0), (30.0, 40.0)]
    assert marathon_shape.week_km(runs, 7) == pytest.approx(10.0)
    assert marathon_shape.week_km(runs, 182) == pytest.approx(70.0)
    assert marathon_shape.week_km([], 182) == 0.0


def test_longjog_points_window_is_strict():
    # 20 km runs: only the one inside the 70-day window scores.
    inside = marathon_shape.longjog_points([(69.9, 20.0)], 60.0)
    assert inside > 0.0
    assert marathon_shape.longjog_points([(70.0, 20.0)], 60.0) == 0.0


def test_longjog_minimum_distance_is_strict():
    # `distance > MIN_KM_FOR_LONGJOG`, so exactly 13 km is not a long jog.
    assert marathon_shape.longjog_points([(1.0, 13.0)], 60.0) == 0.0
    assert marathon_shape.longjog_points([(1.0, 13.5)], 60.0) > 0.0


def test_longjog_points_weight_and_distance_term():
    # A run today carries the full weight 2. vdot 60 targets ln(15) * 12 =
    # 32.4966 km, i.e. 19.4966 km once the 13 km minimum is removed.
    assert marathon_shape.longjog_points([(0.0, 20.0)], 60.0) == pytest.approx(
        2.0 * (20.0 - 13.0) ** 2 / 19.4966**2
    )


def test_longjog_points_decay_over_the_window():
    # Weight = 2 - (2/70) * floor(days_ago): a week old is worth less than fresh.
    fresh = marathon_shape.longjog_points([(0.0, 30.0)], 60.0)
    week_old = marathon_shape.longjog_points([(7.0, 30.0)], 60.0)
    assert week_old == pytest.approx(fresh * (2.0 - 2.0 * 7 / 70) / 2.0)


def test_empty_history_scores_zero():
    assert marathon_shape.percentage([], 60.0) == 0


# --- BasicEnduranceTest::testForVO2max30 -----------------------------------


def test_full_scenario_from_upstream():
    # vdot 30, 7-day windows, 10 km long-jog minimum, 0.67/0.33, activities at
    # days 1 (15 km), 3 (15 km), 8 (42 km). Upstream: weekkm-result 30,
    # longjog-result 0.36, weekkm-percentage 0.63, longjog-percentage 0.36,
    # percentage 54. The day-8 marathon is outside both 7-day windows.
    runs = [(1.0, 15.0), (3.0, 15.0), (8.0, 42.0)]

    assert marathon_shape.week_km(runs, 7) == pytest.approx(30.0)
    assert marathon_shape.longjog_points(
        runs, 30.0, days_for_longjogs=7, min_km=10.0
    ) == pytest.approx(0.36, abs=0.01)
    assert (
        marathon_shape.percentage(runs, 30.0, days_for_week_km=7, days_for_longjogs=7, min_km=10.0)
        == 54
    )


def test_upstream_scenario_with_longjogs_only():
    # Same rows, then setPercentageForLongjogs(1): week term drops out,
    # 100 * 0.3553 = 36.
    runs = [(1.0, 15.0), (3.0, 15.0), (8.0, 42.0)]
    assert (
        marathon_shape.percentage(
            runs,
            30.0,
            days_for_week_km=7,
            days_for_longjogs=7,
            min_km=10.0,
            pct_week=0.0,
            pct_lj=1.0,
        )
        == 36
    )


def test_upstream_scenario_with_fourteen_day_longjog_window():
    # Then setDaysToRecognizeForLongjogs(14), weights still 0.0/1.0 from the
    # previous step: the 42 km day-8 run enters the long-jog window,
    # 100 * (4.7921 * 7 / 14) = 240.
    runs = [(1.0, 15.0), (3.0, 15.0), (8.0, 42.0)]
    assert (
        marathon_shape.percentage(
            runs,
            30.0,
            days_for_week_km=7,
            days_for_longjogs=14,
            min_km=10.0,
            pct_week=0.0,
            pct_lj=1.0,
        )
        == 240
    )


def test_upstream_scenario_with_seventy_day_week_window():
    # Then a 70-day week window and setPercentageForLongjogs(0.01): weekkm-result
    # 72 (all three runs), so 0.99 * 0.1516 + 0.01 * 2.3961 -> 17.
    runs = [(1.0, 15.0), (3.0, 15.0), (8.0, 42.0)]
    assert (
        marathon_shape.percentage(
            runs,
            30.0,
            days_for_week_km=70,
            days_for_longjogs=14,
            min_km=10.0,
            pct_week=0.99,
            pct_lj=0.01,
        )
        == 17
    )


# --- MarathonShapeTest::testSimpleExample ----------------------------------


@pytest.mark.parametrize(
    "distance, points, expected",
    [
        (52.0, 1.0, 75),
        (104.0, 0.5, 75),
        (104.0, 1.0, 100),
        (52.0, 2.0, 125),
        (0.0, 2.0, 100),
    ],
)
def test_shape_for_marathonshape_test_cases(distance, points, expected):
    # vdot 60 (weekly target 104, long-jog target 32.5), 7-day windows and
    # 0.50/0.50 weights, i.e. getShapeFor($distance, $points).
    assert (
        marathon_shape.shape_percentage_from_totals(distance, points, 60.0, 0.5, 0.5, 7, 7)
        == expected
    )


# --- Clamp helper -----------------------------------------------------------


def test_clamped_week_days():
    # Category\BasicEndurance::getDaysToConsiderForWeeklyMileage: max(min(182, n), 70).
    assert marathon_shape.clamped_week_days(10) == 70
    assert marathon_shape.clamped_week_days(70) == 70
    assert marathon_shape.clamped_week_days(100) == 100
    assert marathon_shape.clamped_week_days(182) == 182
    assert marathon_shape.clamped_week_days(400) == 182


def test_percentage_uses_given_week_window_verbatim():
    # The clamp is Task 8's job: an explicit 7-day window is not widened to 70.
    # Three 10 km runs (below the 13 km long-jog minimum) give week_km 30, so
    # 30 * 7 / 7 / 47.49 = 0.6317 -> 0.67 * 0.6317 -> 42; a 70-day window
    # would score 4 instead.
    runs = [(1.0, 10.0), (2.0, 10.0), (3.0, 10.0)]
    assert marathon_shape.percentage(runs, 30.0, days_for_week_km=7) == 42
    assert marathon_shape.percentage(runs, 30.0, days_for_week_km=70) == 4
