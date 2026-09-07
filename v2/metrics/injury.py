"""Longest safe run indicator — single-run distance vs rolling 30-day max.

Core heuristic (injury-risk study): max_safe_run = 1.10 × longest_run_last_30_days.
Exceeding 110% of the rolling 30-day max is where injury risk starts climbing
sharply (~64% increase in the 10-30% range). A second tier at 130% marks
where risk roughly doubles.

Only running and treadmill activities count (strength/cross are excluded).
"""

from datetime import timedelta

import pandas as pd

from normalize import Activity


def _running_activities(activities: list[Activity]) -> list[Activity]:
    return [a for a in activities if a.sport in ("running", "treadmill")]


def longest_run_30d(activities: list[Activity], window_days: int = 30) -> pd.Series:
    """Rolling 30-day maximum run distance (km) for running+treadmill activities.

    Returns a daily series where each date's value is the max distance among
    all running/treadmill activities in the trailing `window_days` calendar days
    (inclusive of the date itself).
    """
    runs = _running_activities(activities)
    if not runs:
        return pd.Series([], dtype=float)

    idx = pd.DatetimeIndex([a.date for a in runs])
    dist = pd.Series([a.distance_km for a in runs], index=idx)
    # multiple runs on same day → take the longest
    dist = dist.groupby(dist.index).max()

    full_idx = pd.date_range(dist.index.min(), dist.index.max(), freq="D")
    dist = dist.reindex(full_idx, fill_value=0.0)
    rolling_max = dist.rolling(window_days, min_periods=1).max()
    rolling_max.index.name = "date"
    return rolling_max


def max_run_ratio(
    activities: list[Activity], window_days: int = 30, min_baseline: int = 7
) -> pd.Series:
    """Ratio of each run's distance to the *prior* 30-day rolling max.

    The rolling max is shifted forward by one day, so the max at date D
    represents the longest run in the 30 days *before* D. A value > 1.0
    means the run exceeded the recent ceiling — > 1.10 is elevated risk,
    > 1.30 is high risk per the injury study.

    For bootstrapping: if fewer than `min_baseline` running days exist in
    the prior window for a given date, the ratio is NaN (no reliable
    baseline). Once `min_baseline` or more days accumulate, the ratio
    becomes available.
    """
    runs = _running_activities(activities)
    if not runs:
        return pd.Series([], dtype=float)

    rolling_max = longest_run_30d(activities, window_days)
    # Shift so max at day D = max of days D-30..D-1 (prior 30 days, no today)
    prior_max = rolling_max.shift(1)

    rows = []
    for a in runs:
        d = pd.Timestamp(a.date)
        if d not in prior_max.index:
            continue
        rmax = prior_max.loc[d]
        if pd.isna(rmax) or rmax <= 0:
            continue
        # Count distinct running days in the *prior* window for baseline check
        window_start = a.date - timedelta(days=window_days)
        running_days_prior = sum(
            1
            for r in runs
            if window_start <= r.date < a.date
        )
        if running_days_prior < min_baseline:
            continue  # not enough baseline — skip (NaN via absence)
        rows.append((d, a.distance_km / rmax))

    if not rows:
        return pd.Series([], dtype=float)

    idx = pd.DatetimeIndex([d for d, _ in rows])
    s = pd.Series([v for _, v in rows], index=idx)
    # multiple runs on same day → take the max ratio (worst case)
    s = s.groupby(s.index).max()
    s.index.name = "date"
    s.name = "max_run_ratio"
    return s
