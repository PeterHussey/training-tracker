"""Marathon-preparedness percentage: Runalyze's BasicEndurance / MarathonShape.

Ported from Runalyze 4.3.x (support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820), inc/core/Calculation/BasicEndurance.php and inc/core/Sports/Running/MarathonShape.php, AGPL.

A value of 100 means training sufficient for an optimal marathon; values above
100 are possible. Two weighted terms make up the score:

* weekly mileage over the last ``days_for_week_km`` days (default 182, never
  fewer than 70) against ``VO2max ** 1.135``;
* long-jog quality over the last ``days_for_longjogs`` days (default 70),
  where every jog above 13 km scores proportionally to its excess distance
  over that minimum, linearly down-weighted with age.

Both terms are normalized to a *week* before weighting, so a long window and a
short one are comparable. Runs are ``(days_ago, distance_km)`` tuples relative
to the as-of moment; the module never sees timestamps, which keeps as-of
evaluation pure. Following ``BasicEndurance::asArray``, the targets always
resolve from the *current* effective VO2max, even for a historical as-of date.

The 70-day week-mileage floor is keyed on ``START_TIME`` (account registration)
upstream; here it is just the :func:`clamped_week_days` helper, and Task 8
passes the caller's own day count to :func:`percentage` verbatim.

Pure computation only: no DB, no network, no module-level state.
"""

import math

from metrics.daniels_gilbert import round_half_up

# Configuration defaults, src/CoreBundle/Component/Configuration/Category/BasicEndurance.php.
MINIMAL_EFFECTIVE_VO2MAX = 25.0  # [ml/kg/min]
MIN_KM_FOR_LONGJOG = 13.0  # [km]
DAYS_FOR_WEEK_KM = 182
DAYS_FOR_WEEK_KM_MIN = 70
DAYS_FOR_LONGJOGS = 70
PERCENTAGE_WEEK_KM = 0.67
PERCENTAGE_LONGJOGS = 0.33

DAYS_PER_WEEK = 7.0


def target_week_km(vdot: float) -> float:
    """Weekly mileage [km] a marathon-ready runner needs at ``vdot``.

    ``pow(max(vdot, 25.0), 1.135)``; values below the 25.0 floor are treated
    as 25.0.
    """
    return max(vdot, MINIMAL_EFFECTIVE_VO2MAX) ** 1.135


def target_lj_km_week_real(vdot: float) -> float:
    """Long-jog distance [km] per week a marathon-ready runner needs.

    ``ln(max(vdot, 25.0) / 4) * 12``, including the 13 km minimum. Values below
    the 25.0 floor are treated as 25.0.
    """
    return math.log(max(vdot, MINIMAL_EFFECTIVE_VO2MAX) / 4.0) * 12.0


def target_lj_km_week_reduced(vdot: float) -> float:
    """As :func:`target_lj_km_week_real`, with the default 13 km minimum removed.

    This is the denominator of the long-jog points formula for the default
    configuration (``getTargetLongjogKmPerWeek``).
    """
    return target_lj_km_week_real(vdot) - MIN_KM_FOR_LONGJOG


def week_km(runs: list[tuple[float, float]], days_for_week_km: int) -> float:
    """Total distance [km] of runs inside the week-mileage window.

    Strict lower bound (``time > timestamp - days * 86400``): a run counts when
    ``days_ago < days_for_week_km``. The window is used as given; clamping is
    the caller's job (:func:`clamped_week_days`).
    """
    total = 0.0
    for days_ago, distance_km in runs:
        if days_ago < days_for_week_km:
            total += distance_km
    return total


def longjog_points(
    runs: list[tuple[float, float]],
    vdot: float,
    days_for_longjogs: int = DAYS_FOR_LONGJOGS,
    min_km: float = MIN_KM_FOR_LONGJOG,
) -> float:
    """Weighted long-jog points, one point per week matching the target.

    A run counts when ``days_ago < days_for_longjogs`` (strict) and
    ``distance_km > min_km`` (strict). Its weight decays linearly from 2.0 for a
    run today towards 0 at the end of the window::

        (2 - (2 / days_for_longjogs) * floor(days_ago))
        * ((distance_km - min_km) ** 2 / reduced_target ** 2)

    The reduced target is ``target_lj_km_week_real(vdot) - min_km`` (i.e.
    :func:`target_lj_km_week_reduced` for the default 13 km minimum): the
    minimum distance is already subtracted, exactly as in
    ``BasicEndurance::getTargetLongjogKmPerWeek``.
    """
    reduced_target = target_lj_km_week_real(vdot) - min_km
    target_squared = reduced_target * reduced_target

    points = 0.0
    for days_ago, distance_km in runs:
        if days_ago >= days_for_longjogs or distance_km <= min_km:
            continue
        weight = 2.0 - (2.0 / days_for_longjogs) * math.floor(days_ago)
        points += weight * (distance_km - min_km) ** 2 / target_squared
    return points


def shape_percentage_from_totals(
    week_km: float,
    lj_points: float,
    vdot: float,
    pct_week: float,
    pct_lj: float,
    days_for_week_km: int,
    days_for_longjogs: int,
) -> int:
    """Marathon shape ``[0, inf)`` from already aggregated totals.

    ``MarathonShape::getShapeFor``: both terms are normalized to a week, then
    weighted and rounded PHP-style.
    """
    week_fraction = week_km * DAYS_PER_WEEK / days_for_week_km / target_week_km(vdot)
    longjog_fraction = lj_points * DAYS_PER_WEEK / days_for_longjogs

    return round_half_up(100.0 * (pct_week * week_fraction + pct_lj * longjog_fraction))


def percentage(
    runs: list[tuple[float, float]],
    vdot: float,
    *,
    days_for_week_km: int = DAYS_FOR_WEEK_KM,
    days_for_longjogs: int = DAYS_FOR_LONGJOGS,
    min_km: float = MIN_KM_FOR_LONGJOG,
    pct_week: float = PERCENTAGE_WEEK_KM,
    pct_lj: float = PERCENTAGE_LONGJOGS,
) -> int:
    """Marathon shape ``[0, inf)`` at ``vdot`` for a run history.

    ``days_for_week_km`` is used verbatim: apply :func:`clamped_week_days` first
    when the history is shorter than the configured minimum window.
    """
    return shape_percentage_from_totals(
        week_km(runs, days_for_week_km),
        longjog_points(runs, vdot, days_for_longjogs, min_km),
        vdot,
        pct_week,
        pct_lj,
        days_for_week_km,
        days_for_longjogs,
    )


def clamped_week_days(days_since_first: float) -> int:
    """Week-mileage window [days] for a history of ``days_since_first`` days.

    ``max(min(182, n), 70)``, matching
    ``Category\\BasicEndurance::getDaysToConsiderForWeeklyMileage``. ``n`` is a
    whole-day count upstream (Task 8 supplies ``ceil`` of the first-activity
    difference).
    """
    return int(min(DAYS_FOR_WEEK_KM, max(DAYS_FOR_WEEK_KM_MIN, days_since_first)))
