"""Lactate threshold (HR anchored) + approximate critical speed.

Research brief 2.2: LT heart rate is the trustworthy anchor (~7% error); LT
pace can overestimate 20-26%. Critical speed is approximated here from the
fastest 1-mile split per session — a trend signal, NOT a lab-derived CS.
"""

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from normalize import Activity


def parse_details_series(
    details: dict, sample_s: float = 1.0
) -> tuple[list[float | None], list[float | None]]:
    """Extract aligned HR and speed lists from a Garmin activity-details payload.

    Args:
        details: Raw details dict with a ``metrics`` list of per-sample dicts.
            Each sample may contain ``heartRate`` and ``speed`` (both float | None).
        sample_s: Nominal interval between samples in seconds. Assumed 1 Hz when
            ``maxChartSize=2000`` downsampling is uniform — caller is responsible
            for passing the correct value if the source uses a different rate.

    Returns:
        ``(hr, speed)`` — two lists of equal length. ``None`` values represent
        optical HR dropouts or GPS gaps.
    """
    metrics = details.get("metrics") or []
    hr: list[float | None] = []
    speed: list[float | None] = []
    for m in metrics:
        hr.append(m.get("heartRate"))
        speed.append(m.get("speed"))
    return hr, speed


def best_window(
    hr: list[float | None],
    speed: list[float | None],
    window_s: int,
    sample_s: float = 1.0,
    max_gap_s: float = 5.0,
) -> dict | None:
    """Find the fastest contiguous window of a given duration.

    Slides a window of ``window_s`` seconds over the sample lists, tracking
    contiguous valid samples (no ``None`` in either HR or speed). A gap longer
    than ``max_gap_s`` seconds (consecutive ``None`` samples) breaks contiguity.

    Args:
        hr: Per-sample heart rate (may contain None for dropouts).
        speed: Per-sample speed in m/s (may contain None for gaps).
        window_s: Desired window duration in seconds.
        sample_s: Seconds per sample (default 1.0 for 1 Hz).
        max_gap_s: Maximum allowed gap (consecutive None samples × sample_s)
            before a window is considered invalid.

    Returns:
        ``{mean_speed, mean_hr, start_idx}`` for the fastest valid window, or
        ``None`` if no fully-covered contiguous window exists.
    """
    n = len(hr)
    if n != len(speed) or n == 0:
        return None

    win = int(window_s / sample_s)
    max_gap = int(max_gap_s / sample_s)
    if max_gap < 1:
        max_gap = 1

    valid = [h is not None and s is not None for h, s in zip(hr, speed, strict=True)]

    best_speed = -1.0
    best_result: dict | None = None

    # Track contiguity in the sliding window.
    # gap_count counts consecutive invalid samples currently inside the window.
    # valid_count counts valid samples currently inside the window.
    gap_count = 0
    valid_count = 0
    # We also need to know the run of trailing None's to detect when a gap
    # enters/exits the window. Store the validity of each position for that.
    gap_streak = 0  # current streak of consecutive None at the tail of window

    for i in range(n):
        # Add new element at right edge of window
        if valid[i]:
            gap_streak = 0
            valid_count += 1
        else:
            gap_streak += 1
            if gap_streak <= max_gap:
                gap_count += 1  # gap still within tolerance

        # Once we have a full window, evaluate it
        if i >= win - 1:
            start = i - win + 1
            # A window is valid only if:
            # 1. It has enough valid samples (all valid, no overflow gaps)
            # 2. No gap inside exceeds max_gap_s
            if valid_count == win and gap_count == 0:
                seg_speed = speed[start : start + win]
                seg_hr = hr[start : start + win]
                mean_s = sum(v for v in seg_speed if v is not None) / win
                mean_h = sum(v for v in seg_hr if v is not None) / win
                if mean_s > best_speed:
                    best_speed = mean_s
                    best_result = {
                        "mean_speed": mean_s,
                        "mean_hr": mean_h,
                        "start_idx": start,
                    }

            # Recount for next iteration: recompute gap streaks over
            # [start+1 .. i+1]. O(win) per step; fine for n ≤ 2000.
            new_start = start + 1
            valid_count = 0
            gap_count = 0
            streak = 0
            for j in range(new_start, i + 1):
                if valid[j]:
                    valid_count += 1
                    streak = 0
                else:
                    streak += 1
                    if streak <= max_gap:
                        gap_count += 1
            gap_streak = streak

    return best_result


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
