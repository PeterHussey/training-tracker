"""Elevation as context + load-diversity (research brief 4.2). Never a risk number."""

import pandas as pd

from normalize import Activity


def _runs(activities: list[Activity], group: str) -> list[Activity]:
    if group == "running":
        return [a for a in activities if a.sport == "running"]
    if group == "treadmill":
        return [a for a in activities if a.sport == "treadmill"]
    return [a for a in activities if a.sport == "cross"]


def daily_elevation_gain(activities: list[Activity], group: str = "running") -> pd.Series:
    rows = []
    for a in _runs(activities, group):
        if a.ele_gain_m is None:
            continue
        rows.append((a.date, a.ele_gain_m))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).sum().sort_index()


def rolling_28d(daily_gain: pd.Series) -> pd.Series:
    full = daily_gain.sort_index()
    if full.empty:
        return full
    full_idx = pd.date_range(full.index.min(), full.index.max(), freq="D")
    full = full.reindex(full_idx, fill_value=0.0)
    return full.rolling(28, min_periods=1).sum()


def gain_per_km(activities: list[Activity], group: str = "running") -> pd.Series:
    rows = []
    for a in _runs(activities, group):
        km = a.distance_km
        if a.ele_gain_m is None or km <= 0:
            continue
        rows.append((a.date, a.ele_gain_m / km))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).mean().sort_index()
