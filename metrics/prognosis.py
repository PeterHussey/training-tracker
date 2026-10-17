"""Race-time prognosis for a distance from an effective VO2max.

Ported from Runalyze 4.3.x (support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820), inc/core/Sports/Running/Prognosis/VO2max.php, AGPL; formulas by Daniels/Gilbert.

``VO2max::getSeconds()`` has no closed form: the Daniels-Gilbert estimate
(:mod:`metrics.daniels_gilbert`) is not analytically invertible in time, so the
race time is found by bisecting the estimate down to the requested VO2max
(``Numerics/Bisection``, :func:`metrics.numerics.find_value`). The PHP's own
bounds are the search interval:

    s ∈ [round(2 * 60 * distance), round(10 * 60 * distance)]

Only VO2max values in ``[15, 90]`` are considered reasonable; anything outside
is a ``null`` prognosis rather than a number, so an invalid vdot never renders
as a plausible-looking time.

The optional marathon-shape adjustment exists because a prognosis straight from
a short-race vdot is optimistic over long distances. The damping factor is a
multiplier on the **vdot**, applied before the bisection -- never a correction
of the resulting seconds:

    factor = min(1.0, 0.6 + 0.4 * max(0.0, 1 - (distance**1.23 - shape_pct)/100))

so a runner who has not trained for the distance is predicted at as little as
60 % of their vdot, and a fully prepared one (shape >= ``distance**1.23``) is
predicted from the raw value. Because the guard in ``getPrognosisInSecondsFor``
is applied to whatever value reaches it, and ``getSeconds`` hands it the
*adjusted* vdot, an out-of-range raw vdot can still be prognosed once damped
into range -- and an in-range one is rejected once damped out of it.

Pure computation only: no DB, no network, no module-level state.
"""

from metrics import daniels_gilbert, numerics

# Race distances the plugin's prognosis panel plots [km].
DISTS: list[float] = [1.0, 3.0, 5.0, 10.0, 21.1, 42.2, 50.0]

# VO2max.php: values below/above are considered invalid.
REASONABLE_MIN: float = 15.0
REASONABLE_MAX: float = 90.0

# VO2max.php::getAdjustmentFactor bounds: the factor is clamped to [0.6, 1.0].
_FACTOR_MIN = 0.6
_FACTOR_RANGE = 0.4
_REQUIRED_SHAPE_EXPONENT = 1.23

# Bounds of the bisection interval, as seconds of race time per km.
_LOWER_SECONDS_PER_KM = 2.0 * 60.0
_UPPER_SECONDS_PER_KM = 10.0 * 60.0


def adjustment_factor(distance_km: float, shape_pct: float | None) -> float:
    """Marathon-shape damping factor for ``distance_km`` [0.6 .. 1.0].

    ``VO2max::getAdjustmentFactor``: the shape a runner would need for the
    distance is taken as ``distance_km ** 1.23`` percent; the shortfall against
    the achieved ``shape_pct`` costs 0.4 of the factor per 100 points, floored
    at 0.6. A shape above the requirement is not a bonus, only the absence of a
    penalty, so the factor is capped at 1.0.

    ``shape_pct=None`` is this module's spelling of the PHP's
    ``adjustForMarathonShape(false)``: no damping at all, i.e. ``1.0``.
    """
    if shape_pct is None:
        return 1.0

    required_shape = distance_km**_REQUIRED_SHAPE_EXPONENT
    shape_factor = max(0.0, 1.0 - (required_shape - shape_pct) / 100.0)

    return min(1.0, _FACTOR_MIN + _FACTOR_RANGE * shape_factor)


def _prognosis_seconds_for(effective_vo2max: float, distance_km: float) -> float | None:
    """``VO2max::getPrognosisInSecondsFor``: bisect ``distance_km`` race time.

    ``None`` -- never a number -- when ``effective_vo2max`` falls outside
    ``[REASONABLE_MIN, REASONABLE_MAX]``.
    """
    if not REASONABLE_MIN <= effective_vo2max <= REASONABLE_MAX:
        return None

    # PHP rounds the bounds with round(); `round_half_up` reproduces it.
    lower = daniels_gilbert.round_half_up(_LOWER_SECONDS_PER_KM * distance_km)
    upper = daniels_gilbert.round_half_up(_UPPER_SECONDS_PER_KM * distance_km)

    return numerics.find_value(
        lambda seconds: daniels_gilbert.estimate_from_race_result(distance_km, seconds),
        effective_vo2max,
        lower,
        upper,
    )


def prognosis_seconds(
    vdot: float, distance_km: float, marathon_shape_pct: float | None = None
) -> float | None:
    """Race time [s] for ``distance_km`` at an effective ``vdot``.

    ``marathon_shape_pct`` damps the vdot by :func:`adjustment_factor` before
    the bisection; ``None`` leaves it undamped. Either way the damped value is
    what the reasonableness guard sees, and ``None`` means "no valid prognosis"
    (PHP's ``null``).
    """
    adjusted = vdot * adjustment_factor(distance_km, marathon_shape_pct)

    return _prognosis_seconds_for(adjusted, distance_km)


def prognosis_table(vdot: float, marathon_shape_pct: float | None = None) -> list[dict]:
    """One row per :data:`DISTS` entry, raw and damped race times.

    The row shape is stable: ``distance_km``, ``raw_seconds`` and
    ``damped_seconds`` are always present. ``raw_seconds`` is the undamped
    prognosis; ``damped_seconds`` is the prognosis at ``marathon_shape_pct``,
    or ``None`` when no shape was given (nothing to damp with) or when the
    damped vdot left the reasonable range. A ``None`` time is a real "no
    prognosis", never a placeholder.
    """
    table = []
    for distance_km in DISTS:
        table.append(
            {
                "distance_km": distance_km,
                "raw_seconds": prognosis_seconds(vdot, distance_km),
                "damped_seconds": (
                    None
                    if marathon_shape_pct is None
                    else prognosis_seconds(vdot, distance_km, marathon_shape_pct)
                ),
            }
        )

    return table
