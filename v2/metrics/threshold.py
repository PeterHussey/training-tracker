"""Lactate threshold (HR anchored) + approximate critical speed.

Research brief 2.2: LT heart rate is the trustworthy anchor (~7% error); LT
pace can overestimate 20-26%. Critical speed is approximated here from the
fastest 1-mile split per session — a trend signal, NOT a lab-derived CS.
"""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from normalize import Activity


@dataclass
class LTData:
    hr: int | None
    speed_m_s: float | None
    date: str | None


def parse_lt(payload: dict) -> LTData:
    sah = payload.get("speed_and_heart_rate") or {}
    return LTData(
        hr=sah.get("heartRate"),
        speed_m_s=sah.get("speed"),
        date=sah.get("calendarDate"),
    )


def approx_cs_1609(activities: list[Activity]) -> pd.Series:
    rows = []
    for a in activities:
        if a.sport not in ("running", "treadmill") or not a.fastest_split_1609:
            continue
        rows.append((a.date, 1609.0 / a.fastest_split_1609))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()


def _sustained_efforts(activities: list[Activity], profile, min_duration_s: float = 1200.0,
                       min_hr_pct: float = 0.85) -> list[Activity]:
    """Filter activities for sustained efforts at high HR intensity.

    Args:
        activities: List of activities to filter
        profile: RunnerProfile with hrmax, hrrest
        min_duration_s: Minimum duration in seconds (default 20 min = 1200s)
        min_hr_pct: Minimum avg HR as fraction of HRmax (default 0.85)
    """
    if profile.hrmax <= profile.hrrest:
        return []
    threshold_hr = profile.hrmax * min_hr_pct
    out = []
    for a in activities:
        if (a.duration_s >= min_duration_s and a.avg_hr is not None
                and a.avg_hr >= threshold_hr):
            out.append(a)
    return out


def rolling_lt_hr(activities: list[Activity], profile, window_days: int = 30) -> pd.Series:
    """30-day rolling LT HR from sustained high-HR efforts.

    Returns a pd.Series indexed by date with estimated LT HR (bpm).
    Computed per-day from sustained efforts in the trailing window_days.
    """
    if not activities:
        return pd.Series([], dtype=float)

    # Filter to load sports with HR data
    load_sports = {"running", "treadmill", "cross"}
    acts = [a for a in activities
            if a.sport in load_sports and a.avg_hr is not None]
    if not acts:
        return pd.Series([], dtype=float)

    acts_by_date = {a.date: a for a in acts}
    dates = sorted(acts_by_date.keys())
    if not dates:
        return pd.Series([], dtype=float)

    results = []
    for _i, d in enumerate(dates):
        window_start = d - timedelta(days=window_days)
        window_acts = [acts_by_date[dd] for dd in dates
                       if window_start <= dd <= d]
        sustained = _sustained_efforts(window_acts, profile)
        if len(sustained) >= 2:  # require at least 2 sustained efforts
            avg_hr = sum(a.avg_hr for a in sustained) / len(sustained)
            results.append((d, avg_hr))

    if not results:
        return pd.Series([], dtype=float)
    return pd.Series([v for _, v in results], index=pd.DatetimeIndex([d for d, _ in results])).sort_index()


def rolling_lt_pace(activities: list[Activity], window_days: int = 45) -> pd.Series:
    """45-day rolling LT pace from critical speed approximation.

    Returns a pd.Series indexed by date with estimated LT pace (m/s).
    Uses fastest 1-mile splits from running+treadmill activities.
    """
    if not activities:
        return pd.Series([], dtype=float)

    load_sports = {"running", "treadmill"}
    acts = [a for a in activities
            if a.sport in load_sports and a.fastest_split_1609 is not None]
    if not acts:
        return pd.Series([], dtype=float)

    acts_by_date = {a.date: a for a in acts}
    dates = sorted(acts_by_date.keys())
    if not dates:
        return pd.Series([], dtype=float)

    results = []
    for d in dates:
        window_start = d - timedelta(days=window_days)
        window_acts = [acts_by_date[dd] for dd in dates
                       if window_start <= dd <= d]
        cs = approx_cs_1609(window_acts)
        if not cs.empty:
            # Use the most recent CS value in the window
            results.append((d, cs.iloc[-1]))

    if not results:
        return pd.Series([], dtype=float)
    return pd.Series([v for _, v in results], index=pd.DatetimeIndex([d for d, _ in results])).sort_index()
