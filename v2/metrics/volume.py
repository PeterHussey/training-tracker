"""Weekly volume / consistency metrics (research brief 4.1)."""
import pandas as pd

from normalize import Activity


def groups() -> list[str]:
    return ["total", "running", "treadmill", "cross"]


def _group_members(activities: list[Activity], group: str) -> list[Activity]:
    if group == "total":
        return activities
    if group == "running":
        return [a for a in activities if a.sport == "running"]
    if group == "treadmill":
        return [a for a in activities if a.sport == "treadmill"]
    return [a for a in activities if a.sport == "cross"]


def weekly_distance(activities: list[Activity], group: str) -> pd.Series:
    acts = _group_members(activities, group)
    if not acts:
        return pd.Series([], dtype=float)
    idx = pd.DatetimeIndex([a.date for a in acts]).strftime("%G-W%V")
    s = pd.Series([a.distance_km for a in acts], index=idx)
    out = s.groupby(s.index).sum().sort_index()
    out.index.name = "week"
    return out


def rolling_4wk(weekly: pd.Series) -> pd.Series:
    return weekly.rolling(4, min_periods=1).mean()


def week_over_week_pct(weekly: pd.Series) -> pd.Series:
    prev = weekly.shift(1)
    return (weekly - prev) / prev.replace(0, pd.NA)
