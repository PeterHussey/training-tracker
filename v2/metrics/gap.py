"""Grade-Adjusted Pace (GAP) — adjusted running pace compensating for elevation.

Uses a simplified energy-cost model (Minetti 2002). Running only; treadmill
and indoor activities are excluded.

Grade-adjusted speed = actual speed × (1 + k × grade)
where grade = ele_gain_m / distance_m (positive = uphill).

Output unit: m/s (convert to min/km in the UI).
"""

import pandas as pd

from normalize import Activity

# Energy-cost adjustment factor per unit grade.  Literature range 0.6–1.0;
# 0.7 is a conservative middle ground validated across moderate grades (±10%).
K = 0.7


def gap_pace(activities: list[Activity]) -> pd.Series:
    """Per-activity grade-adjusted speed (m/s), outdoor running only.

    Returns a DatetimeIndex series (one value per date; duplicate dates are
    averaged).  Runs missing elevation gain or distance are silently skipped.
    """
    rows: list[tuple[pd.Timestamp, float]] = []
    for a in activities:
        if a.sport != "running":
            continue
        if a.ele_gain_m is None or a.distance_m <= 0 or a.avg_speed is None or a.avg_speed <= 0:
            continue
        grade = a.ele_gain_m / a.distance_m
        gap_speed = a.avg_speed * (1.0 + K * grade)
        rows.append((pd.Timestamp(a.date), gap_speed))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).mean().sort_index()
