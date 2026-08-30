"""VO2max trend series — INGESTED from Garmin per-run vO2MaxValue.

Research brief 2.1: Firstbeat estimate (MAPE ~5%, higher at elite levels,
underestimates >=60 mL/kg/min). Never recompute. Flags surface HRmax source
and the device caveat in the series metadata (pipeline layer).
"""
import pandas as pd

from normalize import Activity


def daily_vo2max(activities: list[Activity]) -> pd.Series:
    rows = [
        (a.date, float(a.vo2max))
        for a in activities
        if a.sport == "running" and a.vo2max is not None
    ]
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()
