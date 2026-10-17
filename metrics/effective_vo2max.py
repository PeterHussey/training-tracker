"""Legacy per-activity effective VO2max (pace + heart rate) and race corrector.

Ported from Runalyze 4.3.x
(support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820),
inc/core/Calculation/JD/LegacyEffectiveVO2max.php, AGPL.

Also ports the best-of-three race corrector from
inc/core/Calculation/JD/LegacyEffectiveVO2maxCorrector.php (same commit),
AGPL.

Pure computation only: no DB, no network, no module-level state. The PHP
class stores ``Value`` and applies the corrector in ``exactValue()``; here the
same behaviour is expressed as functions that return the corrected value
directly.
"""

import math

from metrics import daniels_gilbert

# Regression constants for the HR <-> %VO2max table (verbatim).
_HR_SLOPE = 0.68725
_HR_INTERCEPT = 1.00466

# Corrector DB_LOOKUP_LIMIT: number of (best) races to look at.
CORRECTOR_RACE_LIMIT = 3

# Manual corrector parameter is clamped to this range (repo rule, no UI).
MANUAL_CORRECTOR_MIN = 0.5
MANUAL_CORRECTOR_MAX = 2.0


def percentage_at(hr_in_percent: float) -> float:
    """Expected % of VO2max at a given heart rate, both in [0.0, 1.0]."""
    return math.exp((hr_in_percent - _HR_INTERCEPT) / _HR_SLOPE)


def hr_at(percentage: float) -> float:
    """Expected heart rate [0.0, 1.0] at a given % of VO2max [0.0, 1.0]."""
    return _HR_SLOPE * math.log(percentage) + _HR_INTERCEPT


def _corrector_or_default(corrector: float | None) -> float:
    """PHP ``LegacyEffectiveVO2maxCorrector`` truthiness: unset/0 -> 1.0."""
    if corrector is None or corrector <= 0.0:
        return 1.0
    return corrector


def from_speed(speed_m_per_min: float, corrector: float | None = 1.0) -> float:
    """Effective VO2max from a 100%-vVO2max running velocity [m/min]."""
    value = daniels_gilbert.estimate_from_velocity(speed_m_per_min)
    return _corrector_or_default(corrector) * value


def from_pace_and_hr(
    distance_km: float,
    seconds: float,
    hr_in_percent: float,
    corrector: float | None = 1.0,
) -> float:
    """Effective VO2max from pace plus average heart rate.

    ``hr_in_percent`` is ``avg_hr / HRmax`` in [0.0, 1.0]. Invalid input
    (non-positive distance, zero time, non-positive HR) yields ``0.0``. The
    achieved speed is scaled to the speed expected at 100% vVO2max using the
    HR regression, then run through Daniels-Gilbert.
    """
    if hr_in_percent <= 0.0 or seconds == 0 or distance_km <= 0.0:
        return 0.0

    speed_really_achieved = 60_000.0 * distance_km / seconds  # [m/min]
    speed_estimate_at_100_percent = speed_really_achieved / percentage_at(hr_in_percent)

    return from_speed(speed_estimate_at_100_percent, corrector)


def speed(vo2max: float) -> float:
    """Velocity [m/min] at 100% vVO2max for an effective ``vo2max``."""
    return daniels_gilbert.velocity_from_vo2max(vo2max)


def pace(vo2max: float) -> int:
    """Pace [s/km] at 100% vVO2max; ``0`` for a non-positive ``vo2max``."""
    if vo2max <= 0.0:
        return 0
    return daniels_gilbert.round_half_up(60_000.0 / speed(vo2max))


def pace_at(vo2max: float, percentage: float = 1.0) -> int:
    """Pace [s/km] at ``percentage`` of vVO2max; ``0`` for non-positive ``vo2max``."""
    if vo2max <= 0.0:
        return 0
    return daniels_gilbert.round_half_up(60_000.0 / (percentage * speed(vo2max)))


def race_vdot_by_time(distance_km: float, seconds: float) -> float:
    """VO2max by time for a race result (Daniels-Gilbert race estimate)."""
    return daniels_gilbert.estimate_from_race_result(distance_km, seconds)


def race_ratio(distance_km: float, moving_seconds: float, hr_in_percent: float) -> float:
    """Ratio ``vdot_by_time / vdot_by_hr`` for one race.

    Uses moving time (``duration_s``), never elapsed time (Review Focus 3).
    Returns ``0.0`` when the HR-based training estimate is missing/invalid so
    the caller can skip the row.
    """
    training_vdot = from_pace_and_hr(distance_km, moving_seconds, hr_in_percent)
    if training_vdot <= 0.0:
        return 0.0
    return race_vdot_by_time(distance_km, moving_seconds) / training_vdot


def corrector_factor(
    race_ratios: list[float],
    race_vdots: list[float] | None = None,
) -> float:
    """Race corrector factor: the max ratio over the best 3 races by vdot.

    ``race_ratios`` and ``race_vdots`` are parallel lists. When ``race_vdots``
    is given, the best three races by vdot are selected; otherwise the first
    three ratios are used. Non-positive ratios are dropped. An empty selection
    returns the neutral factor ``1.0`` (matching the PHP ``GlobalFactor``
    fallback when no race yields a positive ratio).
    """
    if race_vdots is None:
        selected = list(race_ratios[:CORRECTOR_RACE_LIMIT])
    else:
        paired = sorted(
            zip(race_vdots, race_ratios, strict=False),
            key=lambda pair: pair[0],
            reverse=True,
        )
        selected = [ratio for _, ratio in paired[:CORRECTOR_RACE_LIMIT]]

    positive = [ratio for ratio in selected if ratio > 0.0]
    if not positive:
        return 1.0
    return max(positive)


def manual_corrector_param(value: float | None) -> float:
    """Clamp an optional manually configured corrector to [0.5, 2.0].

    Unset/non-positive behaves PHP-style: neutral ``1.0``.
    """
    if value is None or value <= 0.0:
        return 1.0
    return min(MANUAL_CORRECTOR_MAX, max(MANUAL_CORRECTOR_MIN, value))
