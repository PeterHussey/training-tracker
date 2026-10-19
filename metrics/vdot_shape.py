"""Daily effective VO2max and the 30-day duration-weighted "shape".

Ported from Runalyze 4.3.x (support/4.3.x @ 5669237a78c7d3512df969052fd0d97e5dca6820), inc/core/Calculation/JD/Shape.php, AGPL.

``Shape.php`` runs one aggregate per as-of day over a window of running
activities (``VO2MAX_DAYS`` = 30):

    SUM(vo2max * s * use_vo2max) / SUM(s * use_vo2max * (vo2max > 0))

where ``s`` is the moving duration in seconds, and ``vo2max`` is the stored
per-activity column -- ``vo2max_with_elevation`` when the elevation correction
is configured on. That column is the pace+HR estimate (Task 3); this module
recomputes it through :mod:`metrics.effective_vo2max`. The ratio is rounded to
five decimals and then multiplied by the race corrector factor.

Two consequences of the SQL above are load-bearing:

* the mean is duration-weighted, so a two-hour long run weighs far more than a
  one-minute entry;
* the denominator counts only rows with a positive value, so an activity whose
  value is 0 (no usable heart rate) does not dilute the mean.

Elevation support follows ``Calculation\\Elevation\\DistanceModifier``: the
correction adds *distance* (+2 m per metre climbed, -1 m per metre descended,
``VO2max.php`` defaults) before the value is estimated -- it never scales the
value itself.

Pure computation only: no DB, no network, no module-level state.
"""

from datetime import datetime, time

import pandas as pd

from metrics import daniels_gilbert, effective_vo2max
from normalize import Activity

# VO2max.php (`VO2MAX_CORRECTION_POSITIVE_ELEVATION` / `..._NEGATIVE_...`)
# defaults: additional distance [m] per metre of elevation [m].
POSITIVE_ELEVATION_CORRECTION = 2
NEGATIVE_ELEVATION_CORRECTION = -1

# Physiologically valid running velocity [m/min], the same window the
# Daniels-Gilbert race-result estimate rejects outside of.
_MIN_VELOCITY = daniels_gilbert.VELOCITY_MIN
_MAX_VELOCITY = daniels_gilbert.VELOCITY_MAX

# `normalize.sport_of` maps Garmin `running` / `treadmill_running` to these.
_RUN_SPORTS = frozenset({"running", "treadmill"})


def _velocity_m_per_min(a: Activity) -> float:
    """Raw pace in m/min, before any heart-rate correction."""
    return 60_000.0 * a.distance_km / a.duration_s


def _additional_distance_km(ele_gain_m: float | None, ele_loss_m: float | None) -> float:
    """Distance added by the elevation correction [km] (PHP ``additionalDistance``).

    Without route up/down data Runalyze's elevation estimate passes the
    activity's single elevation value for BOTH up and down, so a missing
    descent falls back to the ascent rather than 0 (a 0 descent would add
    +2 m of distance per metre climbed with no offset and overshoot).
    """
    up = 0.0 if ele_gain_m is None else ele_gain_m
    down = up if ele_loss_m is None else ele_loss_m
    # A pathological gain of 0/None with a large loss can shrink the corrected
    # distance; Runalyze's arithmetic does the same, and a non-positive
    # distance simply contributes no value.
    return (POSITIVE_ELEVATION_CORRECTION * up + NEGATIVE_ELEVATION_CORRECTION * down) / 1000.0


def _day_end_ts(as_of: datetime) -> float:
    """Epoch seconds of ``as_of``'s day at 23:59:59 (PHP ``setTime(23, 59, 59)``)."""
    return datetime.combine(as_of.date(), time(23, 59, 59)).timestamp()


def _activity_ts(a: Activity) -> float:
    """Epoch seconds of an activity's start.

    Falls back to noon of its calendar date when ``ts_ms`` is unset, so
    activities without a start timestamp still window by day.
    """
    if a.ts_ms:
        return a.ts_ms / 1000.0
    return datetime.combine(a.date, time(12, 0, 0)).timestamp()


def qualifying(activities: list[Activity], hrmax: float | None) -> list[Activity]:
    """Running activities that can carry an effective-VO2max value.

    Keeps running/treadmill activities with a positive distance and duration, an
    average heart rate at or below ``hrmax``, and a raw Daniels-Gilbert velocity
    (``60000 * km / s``, no heart-rate correction) within ``[50, 1000]`` m/min
    inclusive. Without an ``hrmax`` nothing can be qualified.
    """
    if hrmax is None:
        return []

    runs = []
    for a in activities:
        if a.sport not in _RUN_SPORTS:
            continue
        if a.distance_m <= 0.0 or a.duration_s <= 0.0:
            continue
        if a.avg_hr is None or a.avg_hr > hrmax:
            continue
        velocity = _velocity_m_per_min(a)
        if velocity < _MIN_VELOCITY or velocity > _MAX_VELOCITY:
            continue
        runs.append(a)

    return runs


def activity_vo2max(
    a: Activity,
    hrmax: float,
    corrector: float = 1.0,
    *,
    use_elevation: bool = False,
) -> float:
    """Effective VO2max [ml/kg/min] of one activity.

    ``0.0`` when the activity has no average heart rate or ``hrmax`` is not
    usable. With ``use_elevation`` the pace is estimated from the
    elevation-corrected distance (Runalyze's optional correction; off by
    default, as in ``VO2MAX_USE_CORRECTION_FOR_ELEVATION``).
    """
    if a.avg_hr is None or hrmax is None or hrmax <= 0.0:
        return 0.0

    distance_km = a.distance_km
    if use_elevation:
        distance_km += _additional_distance_km(a.ele_gain_m, a.ele_loss_m)

    return effective_vo2max.from_pace_and_hr(distance_km, a.duration_s, a.avg_hr / hrmax, corrector)


def shape_at(
    activities: list[Activity],
    as_of: datetime,
    hrmax: float | None,
    window_days: int = 30,
    corrector: float = 1.0,
    use_elevation: bool = False,
) -> float:
    """Duration-weighted shape over the window ending at ``as_of``'s day end.

    Rows must fall in ``[day end - window_days, day end]`` (both ends inclusive)
    and contribute a positive value, exactly like
    ``Shape.php::mysqlVO2maxSum``/``mysqlVO2maxSumTime``. An empty window -- or a
    window in which no row has a usable value -- is ``0.0``.
    """
    if hrmax is None or hrmax <= 0.0:
        return 0.0

    day_end = _day_end_ts(as_of)
    window_start = day_end - window_days * 86_400

    value_sum = 0.0
    duration_sum = 0.0
    for a in qualifying(activities, hrmax):
        ts = _activity_ts(a)
        if ts > day_end or ts < window_start:
            continue
        value = activity_vo2max(a, hrmax, corrector, use_elevation=use_elevation)
        if value <= 0.0:
            continue
        value_sum += value * a.duration_s
        duration_sum += a.duration_s

    if duration_sum <= 0.0:
        return 0.0

    return round(value_sum / duration_sum, 5)


def shape_series(
    activities: list[Activity],
    hrmax: float | None,
    window_days: int = 30,
    corrector: float = 1.0,
    use_elevation: bool = False,
) -> pd.Series:
    """Daily shape from the first qualifying run through the last activity date.

    One row per calendar day; a day whose window holds no qualifying run is a
    real ``0.0`` so the plotted line stays continuous. Without an ``hrmax`` or
    without any qualifying run there is no shape at all and the series is empty.
    """
    if hrmax is None or hrmax <= 0.0:
        return pd.Series([], dtype=float, name="vdot_shape")

    runs = qualifying(activities, hrmax)
    if not runs:
        return pd.Series([], dtype=float, name="vdot_shape")

    start = min(a.date for a in runs)
    end = max(a.date for a in activities)
    index = pd.date_range(start, end, freq="D")
    values = [
        shape_at(activities, day, hrmax, window_days, corrector, use_elevation=use_elevation)
        for day in index
    ]

    return pd.Series(values, index=index, name="vdot_shape")
