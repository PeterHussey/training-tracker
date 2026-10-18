# tests/test_daniels_gilbert.py
r"""Golden tests for the Daniels–Gilbert VO2max port.

Race-result tables are Runalyze upstream JD\LegacyEffectiveVO2maxTest pairs,
i.e. race *times* (seconds) for each distance. ``estimate_from_race_result``
must reproduce the VO2max those times correspond to.

``pace_s_per_km`` is a *different* quantity: the velocity at VO2max (the
inverse of ``estimate_from_velocity``, no drop-dead divisor), so it is pinned
separately rather than against the race-time tables.
"""

import pytest

from metrics import daniels_gilbert

# --- estimate_from_race_result reproduces upstream VO2max tables -------------

RACE_TABLES = [
    # (distance_km, [(vdot, race_time_s), ...])
    (
        5.0,
        [
            (30, 1840),
            (35, 1620),
            (40, 1448),
            (45, 1310),
            (50, 1197),
            (55, 1102),
            (60, 1023),
            (65, 954),
            (70, 895),
            (75, 843),
            (80, 798),
            (85, 757),
        ],
    ),
    (
        10.0,
        [
            (30, 3826),
            (35, 3363),
            (40, 3003),
            (45, 2716),
            (50, 2481),
            (55, 2286),
            (60, 2122),
            (65, 1981),
            (70, 1860),
            (75, 1754),
            (80, 1661),
            (85, 1579),
        ],
    ),
    (
        21.0975,
        # Upstream LegacyEffectiveVO2maxTest.php test21kResults: vdot 30 is
        # 2*3600+21*60+4 = 8464 s (the brief's "8064" is a transcription typo).
        [
            (30, 8464),
            (35, 7453),
            (40, 6659),
            (45, 6020),
            (50, 5495),
            (55, 5058),
            (60, 4689),
            (65, 4373),
            (70, 4101),
            (75, 3863),
            (80, 3654),
            (85, 3470),
        ],
    ),
    (
        42.195,
        [
            (30, 17357),
            (35, 15363),
            (40, 13785),
            (45, 12506),
            (50, 11449),
            (55, 10561),
            (60, 9805),
            (65, 9155),
            (70, 8590),
            (75, 8095),
            (80, 7658),
            (85, 7270),
        ],
    ),
]


@pytest.mark.parametrize("distance_km,pairs", RACE_TABLES)
def test_race_result_recovers_upstream_vdot(distance_km, pairs):
    for vdot, race_time_s in pairs:
        got = daniels_gilbert.estimate_from_race_result(distance_km, float(race_time_s))
        assert abs(got - vdot) <= 0.1, (distance_km, vdot, race_time_s, got)


# --- estimate_from_velocity guard bounds ------------------------------------


def test_velocity_guard_lives_in_race_result():
    # estimate_from_velocity itself is unguarded (pure formula): just below 50
    # it still returns the (clamped-positive) value.
    assert daniels_gilbert.estimate_from_velocity(49.99) > 0.0

    # The 50..1000 guard lives in estimate_from_race_result. At exactly the
    # boundary velocities the value is computed, not zeroed.
    # 5 km in 6000 s → 50 m/min exactly.
    assert daniels_gilbert.estimate_from_race_result(5.0, 6000.0) > 0.0
    # 5 km in 300 s → 1000 m/min exactly.
    assert daniels_gilbert.estimate_from_race_result(5.0, 300.0) > 0.0
    # Just outside the guard → 0.0.
    assert daniels_gilbert.estimate_from_race_result(5.0, 6001.0) == 0.0  # < 50
    assert daniels_gilbert.estimate_from_race_result(5.0, 299.0) == 0.0  # > 1000


def test_estimate_from_velocity_formula():
    # -4.6 + 0.182253*200 + 0.000104*200**2 = -4.6 + 36.4506 + 4.16 = 36.0106
    assert daniels_gilbert.estimate_from_velocity(200.0) == pytest.approx(36.0106)
    # clamped at zero for low velocities inside the guard
    assert daniels_gilbert.estimate_from_velocity(50.0) == pytest.approx(
        max(0.0, -4.6 + 0.182253 * 50.0 + 0.000104 * 50.0 * 50.0)
    )


def test_evaluate_drop_dead_formula():
    assert daniels_gilbert.evaluate_drop_dead(0.0) == pytest.approx(0.8 + 0.1894393 + 0.2989558)
    # large time → asymptote 0.8
    assert daniels_gilbert.evaluate_drop_dead(1000.0) == pytest.approx(0.8, abs=1e-3)


# --- zero / out-of-range race inputs guard ----------------------------------


def test_zero_inputs_guard():
    assert daniels_gilbert.estimate_from_race_result(0.0, 100.0) == 0.0
    assert daniels_gilbert.estimate_from_race_result(5.0, 0.0) == 0.0


def test_race_result_velocity_outside_guard():
    # too slow: 5 km in 10000 s → 30 m/min
    assert daniels_gilbert.estimate_from_race_result(5.0, 10000.0) == 0.0
    # too fast: 1 km in 30 s → 2000 m/min
    assert daniels_gilbert.estimate_from_race_result(1.0, 30.0) == 0.0


# --- velocity inverse + pace ------------------------------------------------


def test_velocity_from_vo2max_guard():
    assert daniels_gilbert.velocity_from_vo2max(0.0) == 0.0
    assert daniels_gilbert.velocity_from_vo2max(-5.0) == 0.0
    assert daniels_gilbert.velocity_from_vo2max(30.0) > 0.0


def test_velocity_from_vo2max_inverts_estimate_from_velocity():
    # Runalyze's inverse is the analytic root with the vertex approximated as
    # -876 (exact vertex ≈ -876.216), so the round-trip is near-exact but not
    # bit-exact — pin the documented approximation tolerance.
    for vdot in (30.0, 45.0, 60.0, 85.0):
        velocity = daniels_gilbert.velocity_from_vo2max(vdot)
        assert daniels_gilbert.estimate_from_velocity(velocity) == pytest.approx(vdot, abs=0.05)


def test_pace_s_per_km_guard():
    assert daniels_gilbert.pace_s_per_km(None) == 0
    assert daniels_gilbert.pace_s_per_km(0.0) == 0
    assert daniels_gilbert.pace_s_per_km(-5.0) == 0


def test_pace_s_per_km_vdot_30():
    # velocity(30) ≈ 172.84 m/min → 60000/172.84 ≈ 347.2 → 347
    assert daniels_gilbert.pace_s_per_km(30) == 347


def test_round_half_up_not_bankers():
    assert daniels_gilbert.round_half_up(1840.5) == 1841
    # Python's round() would give 1840 (banker's rounding)
    assert round(1840.5) == 1840
    assert daniels_gilbert.round_half_up(2.5) == 3
    assert daniels_gilbert.round_half_up(-2.5) == -3  # half away from zero


def test_pace_s_per_km_matches_round_half_up_of_velocity():
    vdot = 42.0
    velocity = daniels_gilbert.velocity_from_vo2max(vdot)
    assert daniels_gilbert.pace_s_per_km(vdot) == daniels_gilbert.round_half_up(60000.0 / velocity)
