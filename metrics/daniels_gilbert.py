"""Daniels–Gilbert effective-VO2max estimation and its velocity inverse.

Ported from Runalyze 4.3.x
(support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820),
inc/core/Sports/Running/VO2max/Estimation/DanielsGilbertFormula.php and
inc/core/Sports/Running/VO2max/VO2maxVelocity.php, AGPL.

Formulas by Daniels/Gilbert. Pure computation only: no DB, no network, no
module-level state.
"""

import math

VELOCITY_MIN = 50.0  # [m/min] lower guard for a valid race-result velocity
VELOCITY_MAX = 1000.0  # [m/min] upper guard for a valid race-result velocity


def estimate_from_velocity(v_m_per_min: float) -> float:
    """Estimate VO2max [ml/kg/min] from a running velocity [m/min]."""
    return max(0.0, -4.6 + 0.182253 * v_m_per_min + 0.000104 * v_m_per_min * v_m_per_min)


def evaluate_drop_dead(t_min: float) -> float:
    """Drop-dead (duration) factor for a race lasting ``t_min`` minutes."""
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)


def estimate_from_race_result(distance_km: float, seconds: float) -> float:
    """Effective VO2max from a race result, corrected for duration.

    Returns ``0.0`` for non-positive distance/time or when the achieved
    velocity falls outside ``[50, 1000]`` m/min.
    """
    minutes = seconds / 60.0

    # Guard order matches Runalyze's short-circuit: distance and time are
    # checked before dividing by ``minutes``, so seconds <= 0 never divides.
    if distance_km <= 0.0 or minutes <= 0.0:
        return 0.0

    velocity = 1000.0 * distance_km / minutes
    if velocity < VELOCITY_MIN or velocity > VELOCITY_MAX:
        return 0.0

    return estimate_from_velocity(velocity) / evaluate_drop_dead(minutes)


def velocity_from_vo2max(vo2max: float, percentage: float = 1.0) -> float:
    """Running velocity [m/min] at ``percentage`` of an effective ``vo2max``.

    Analytic inverse of :func:`estimate_from_velocity`. Non-positive
    ``vo2max * percentage`` yields ``0.0``.
    """
    scaled = vo2max * percentage
    if scaled <= 0.0:
        return 0.0

    return -876.0 + math.sqrt(876.0 * 876.0 + (4.6 + scaled) / 0.000104)


def round_half_up(x: float) -> int:
    """Round half away from zero, matching PHP's ``round()``.

    Python's built-in ``round()`` rounds halves to even; PHP rounds halves
    away from zero. Kept here so pace integers match Runalyze exactly.
    """
    if x >= 0.0:
        return int(math.floor(x + 0.5))
    return int(math.ceil(x - 0.5))


def pace_s_per_km(vo2max: float | None, percentage: float = 1.0) -> int:
    """Pace [s/km] at ``percentage`` of an effective ``vo2max``.

    ``vo2max`` may be ``None``; a non-positive resulting velocity yields ``0``.
    """
    if vo2max is None:
        return 0

    velocity = velocity_from_vo2max(vo2max, percentage)
    if velocity <= 0.0:
        return 0

    return round_half_up(60000.0 / velocity)
