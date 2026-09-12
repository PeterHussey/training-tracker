"""Daily volume / consistency metrics (research brief 4.1)."""

import pandas as pd

from normalize import Activity


def groups() -> list[str]:
    return ["total", "running", "treadmill", "cross", "strength"]


def _group_members(activities: list[Activity], group: str) -> list[Activity]:
    if group == "total":
        return activities
    if group == "running":
        return [a for a in activities if a.sport == "running"]
    if group == "treadmill":
        return [a for a in activities if a.sport == "treadmill"]
    if group == "strength":
        return [a for a in activities if a.sport == "strength"]
    return [a for a in activities if a.sport == "cross"]


def _running_members(activities: list[Activity], group: str) -> list[Activity]:
    """Running + treadmill only for distance. Cross/strength have no distance."""
    if group == "total":
        return [a for a in activities if a.sport in ("running", "treadmill")]
    if group in ("cross", "strength"):
        return []
    return _group_members(activities, group)


def daily_distance(activities: list[Activity], group: str) -> pd.Series:
    """Daily distance (km). Running + treadmill only — distance has no
    meaning for cross/strength."""
    members = _running_members(activities, group)
    if not members:
        return pd.Series([], dtype=float, name="distance_km")
    idx = pd.DatetimeIndex([a.date for a in members])
    s = pd.Series([a.distance_km for a in members], index=idx, name="distance_km")
    return s.groupby(level=0).sum().sort_index()


def daily_duration_hours(activities: list[Activity], group: str) -> pd.Series:
    """Daily duration (hours). All sports — cross/strength have duration."""
    members = _group_members(activities, group)
    if not members:
        return pd.Series([], dtype=float, name="duration_hours")
    idx = pd.DatetimeIndex([a.date for a in members])
    s = pd.Series([a.duration_min / 60.0 for a in members], index=idx, name="duration_hours")
    return s.groupby(level=0).sum().sort_index()


def resample_agg(s: pd.Series, freq: str) -> pd.Series:
    """Generic resampler. 'W-SUN' → Mon–Sun ISO-week buckets, index shifted
    to Monday. 'ME' → month-end-aligned. Returns summed, sorted-index series.
    Empty buckets (no source data) are dropped."""
    if s.empty:
        return pd.Series([], dtype=float)
    resampled = s.resample(freq).sum()
    # Drop empty buckets (no source data → sum is 0). resample fills 0 for
    # missing weeks; we only want weeks with actual activity data.
    counts = s.resample(freq).count()
    resampled = resampled[counts > 0]
    if freq == "W-SUN":
        # Shift Sunday labels back to Monday (ISO-week convention).
        resampled.index = resampled.index - pd.Timedelta(days=6)
    return resampled.sort_index()


def rolling_4wk(weekly: pd.Series) -> pd.Series:
    return weekly.rolling(4, min_periods=1).mean()


def week_over_week_pct(weekly: pd.Series) -> pd.Series:
    prev = weekly.shift(1)
    return (weekly - prev) / prev.replace(0, pd.NA)
