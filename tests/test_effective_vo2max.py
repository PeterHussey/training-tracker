# tests/test_effective_vo2max.py
r"""Golden tests for the legacy per-activity effective-VO2max port.

Numbers are Runalyze upstream ``JD\LegacyEffectiveVO2maxTest`` pairs plus the
formula/guard constants transcribed from ``LegacyEffectiveVO2max.php`` and
``LegacyEffectiveVO2maxCorrector.php``.
"""

import math

import pytest

from metrics import daniels_gilbert, effective_vo2max

# --- HR <-> %VO2max regression (upstream testHRformula table) ---------------


HR_TABLE = {
    # %VO2max => %HRmax (Daniels, German ed., table 2.2, p. 42f.)
    59: 65,
    60: 66,
    61: 67,
    62: 68,
    63: 69,
    64: 70,
    65: 71,
    66: 72,
    67: 73,
    68: 74,
    69: 75,
    70: 75.5,
    71: 76,
    72: 77,
    73: 78,
    74: 79,
    75: 80,
    76: 81,
    77: 82,
    78: 83,
    79: 84,
    80: 85,
    81: 86,
    82: 87,
    83: 88,
    84: 89,
    85: 89.5,
    86: 90,
    87: 91,
    88: 92,
    89: 92.5,
    90: 93,
    91: 94,
    92: 95,
    93: 96,
    94: 97,
    95: 97.5,
    96: 98,
    97: 98.5,
    98: 99,
    99: 99.5,
    100: 100,
}


def test_hr_formula_matches_upstream_table():
    # Upstream epsilon since v1.5: 1.25 percentage points.
    for vo2max_pct, hr_pct in HR_TABLE.items():
        assert effective_vo2max.hr_at(vo2max_pct / 100) == pytest.approx(
            hr_pct / 100, abs=0.0125
        ), f"hr_at({vo2max_pct})"
        assert effective_vo2max.percentage_at(hr_pct / 100) == pytest.approx(
            vo2max_pct / 100, abs=0.0125
        ), f"percentage_at({hr_pct})"


def test_hr_formula_constants():
    assert effective_vo2max.hr_at(0.5) == pytest.approx(0.68725 * math.log(0.5) + 1.00466)
    assert effective_vo2max.percentage_at(0.5) == pytest.approx(math.exp((0.5 - 1.00466) / 0.68725))


# --- from_pace_and_hr -------------------------------------------------------


def test_distance_independence():
    # upstream: 10 km / 2400 s / hr 0.75 == 3 km / 720 s / hr 0.75
    assert effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75) == pytest.approx(
        effective_vo2max.from_pace_and_hr(3.0, 720, 0.75), abs=1e-6
    )


def test_hr_zero_is_null_not_zero_output():
    assert effective_vo2max.from_pace_and_hr(5.0, 1500, 0.0) == 0.0
    # negative HR is equally invalid
    assert effective_vo2max.from_pace_and_hr(5.0, 1500, -0.1) == 0.0


def test_zero_or_negative_distance_and_time_guard():
    assert effective_vo2max.from_pace_and_hr(0.0, 1500, 0.75) == 0.0
    assert effective_vo2max.from_pace_and_hr(-1.0, 1500, 0.75) == 0.0
    assert effective_vo2max.from_pace_and_hr(5.0, 0, 0.75) == 0.0


def test_corrector_zero_behaves_php_truthiness():
    # 0.0 means "unset" in the PHP constructor -> falls back to GlobalFactor 1.0
    assert effective_vo2max.from_pace_and_hr(5.0, 1500, 0.75, corrector=0.0) == (
        effective_vo2max.from_pace_and_hr(5.0, 1500, 0.75)
    )


def test_corrector_multiplies_exact_value():
    base = effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75)
    assert effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75, corrector=0.9) == (
        pytest.approx(0.9 * base, abs=1e-9)
    )


# --- from_speed / speed / pace ----------------------------------------------


def test_from_speed_upstream_250():
    # upstream testSpeed: 250 m/min = 4:00 min/km; VO2max ~48, pace 240 s/km
    value = effective_vo2max.from_speed(250)
    assert value == pytest.approx(48, abs=0.6)
    assert effective_vo2max.speed(value) == pytest.approx(250, abs=0.1)
    assert effective_vo2max.pace(value) == 240
    assert effective_vo2max.pace_at(value, 1.0) == 240


def test_from_speed_very_low_is_clamped_to_zero():
    # Upstream testVeryLowSpeed: 16.67 m/min ~ 60 min/km. The unguarded
    # estimate_from_velocity clamp (max(0.0, ...)) must return 0, which Task 2
    # could not reach through the race-time velocity guard.
    value = effective_vo2max.from_speed(16.67)
    assert value == 0.0
    assert effective_vo2max.speed(value) == 0.0
    assert effective_vo2max.pace(value) == 0
    assert effective_vo2max.pace_at(value, 1.0) == 0


def test_speed_pace_guard_for_non_positive_value():
    assert effective_vo2max.speed(0.0) == 0.0
    assert effective_vo2max.speed(-5.0) == 0.0
    assert effective_vo2max.pace(0.0) == 0
    assert effective_vo2max.pace_at(-5.0, 0.8) == 0


def test_from_speed_applies_corrector_like_pace_and_hr():
    base = effective_vo2max.from_speed(250)
    assert effective_vo2max.from_speed(250, corrector=0.9) == pytest.approx(0.9 * base, abs=1e-9)


# --- race_vdot_by_time aliases Daniels-Gilbert ------------------------------


def test_race_vdot_by_time_aliases_daniels_gilbert():
    for distance_km, seconds in [(5.0, 1197.0), (42.195, 8590.0), (21.0975, 5495.0)]:
        assert effective_vo2max.race_vdot_by_time(distance_km, seconds) == (
            daniels_gilbert.estimate_from_race_result(distance_km, seconds)
        )


# --- corrector_factor (best 3 by race vdot) ---------------------------------


def test_corrector_factor_best_three_by_race_vdot_default_one():
    # race_vdots with ratios: 60/62=0.9677, 55/53=1.0377, 70/50=1.4, 68/60=1.1333
    ratios = [1.4, 1.1333, 0.9677, 1.0377]
    # best 3 by race vdot: 70, 68, 60 -> ratios 1.4, 1.1333, .9677 -> max 1.4
    assert effective_vo2max.corrector_factor(ratios, race_vdots=[70, 68, 60, 55]) == (
        pytest.approx(1.4)
    )
    assert effective_vo2max.corrector_factor([], []) == 1.0


def test_corrector_factor_without_vdots_takes_first_three():
    # No vdots -> the first three ratios as given are used.
    assert effective_vo2max.corrector_factor([0.9, 1.2, 1.1, 5.0]) == pytest.approx(1.2)


def test_corrector_factor_drops_non_positive_ratios():
    # A zero/negative ratio (e.g. a race whose training vdot is 0) is skipped.
    assert effective_vo2max.corrector_factor([0.0, -0.5, 1.1]) == pytest.approx(1.1)
    assert effective_vo2max.corrector_factor([0.0, 0.0]) == 1.0


# --- race_ratio uses moving seconds (Review Focus 3) ------------------------


def test_race_ratio_uses_moving_seconds():
    # 10 km: moving 2400 s vs elapsed 2500 s at avg HR 0.75 must differ.
    moving = effective_vo2max.race_ratio(10.0, 2400, 0.75)
    elapsed = effective_vo2max.race_ratio(10.0, 2500, 0.75)
    assert moving != pytest.approx(elapsed)


def test_race_ratio_skips_when_training_vdot_missing():
    # HR 0 -> training vdot 0 -> ratio skipped as 0.
    assert effective_vo2max.race_ratio(10.0, 2400, 0.0) == 0.0


def test_race_ratio_is_vdot_by_time_over_training_vdot():
    distance_km, seconds, hr = 10.0, 2400, 0.75
    expected = effective_vo2max.race_vdot_by_time(distance_km, seconds) / (
        effective_vo2max.from_pace_and_hr(distance_km, seconds, hr)
    )
    assert effective_vo2max.race_ratio(distance_km, seconds, hr) == pytest.approx(expected)


# --- manual corrector parameter clamp ---------------------------------------


def test_manual_corrector_param_clamps_to_range():
    assert effective_vo2max.manual_corrector_param(1.0) == 1.0
    assert effective_vo2max.manual_corrector_param(0.1) == 0.5
    assert effective_vo2max.manual_corrector_param(5.0) == 2.0
    assert effective_vo2max.manual_corrector_param(1.3) == pytest.approx(1.3)


def test_manual_corrector_param_unset_is_one():
    assert effective_vo2max.manual_corrector_param(0.0) == 1.0
    assert effective_vo2max.manual_corrector_param(-1.0) == 1.0
