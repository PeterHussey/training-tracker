"""VO2max trend series — INGESTED from Garmin daily trend endpoint.

Research brief 2.1: Firstbeat estimate (MAPE ~5%, higher at elite levels,
underestimates >=60 mL/kg/min). Never recompute. Flags surface HRmax source
and the device caveat in the series metadata (pipeline layer).

The trend comes from `/metrics-service/metrics/maxmet/daily/{start}/{end}`
which returns daily historical VO2max values (unlike `/maxmet/latest` which
always returns the most recent value). We prefer `vo2MaxPreciseValue` for
trend granularity and fall back to the rounded `vo2MaxValue` if absent.
"""

import pandas as pd

from normalize import Activity


def daily_vo2max_from_trend(trend_payload: list[dict]) -> pd.Series:
    """Build a daily VO2max series from the /maxmet/daily trend endpoint.

    Each entry in the payload carries a 'generic' (running) object with
    'calendarDate', 'vo2MaxValue' (rounded int) and 'vo2MaxPreciseValue'
    (decimal). Uses the precise value when present, else the rounded value.
    """
    rows: list[tuple[pd.Timestamp, float]] = []
    for entry in trend_payload or []:
        generic = (entry or {}).get("generic") or {}
        date_str = generic.get("calendarDate")
        if not date_str:
            continue
        v = generic.get("vo2MaxPreciseValue")
        if v is None:
            v = generic.get("vo2MaxValue")
        if v is None:
            continue
        rows.append((pd.Timestamp(date_str), float(v)))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()


def daily_vo2max(activities: list[Activity]) -> pd.Series:
    """Legacy fallback: per-run vO2MaxValue (constant across runs, no variance).

    Prefer daily_vo2max_from_trend for a real trend; this exists only as a
    fallback when no trend payload is available.
    """
    rows = [
        (a.date, float(a.vo2max))
        for a in activities
        if a.sport == "running" and a.vo2max is not None
    ]
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()
