"""Lactate threshold (HR anchored) + approximate critical speed.

Research brief 2.2: LT heart rate is the trustworthy anchor (~7% error); LT
pace can overestimate 20-26%. Critical speed is approximated here from the
fastest 1-mile split per session — a trend signal, NOT a lab-derived CS.
"""

from dataclasses import dataclass

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

    Slides a window of ``window_s`` seconds over the sample lists.  Gaps
    (consecutive ``None`` samples) up to ``max_gap_s`` seconds are tolerated;
    only the valid (non-``None``) samples are used for mean speed / HR
    calculation.  A gap exceeding ``max_gap_s`` seconds invalidates the window.

    Args:
        hr: Per-sample heart rate (may contain None for dropouts).
        speed: Per-sample speed in m/s (may contain None for gaps).
        window_s: Desired window duration in seconds.
        sample_s: Seconds per sample (default 1.0 for 1 Hz).
        max_gap_s: Maximum allowed gap (consecutive None samples × sample_s)
            before a window is considered invalid.

    Returns:
        ``{mean_speed, mean_hr, start_idx}`` for the fastest valid window, or
        ``None`` if no valid window exists.
    """
    n = len(hr)
    if n != len(speed) or n == 0:
        return None

    win = int(window_s / sample_s)
    max_gap = int(max_gap_s / sample_s)

    valid = [h is not None and s is not None for h, s in zip(hr, speed, strict=True)]

    # Prefix sums — O(n) build, O(1) per window query.
    prefix_valid = [0] * (n + 1)
    prefix_speed = [0.0] * (n + 1)
    prefix_hr = [0.0] * (n + 1)
    for i in range(n):
        prefix_valid[i + 1] = prefix_valid[i] + (1 if valid[i] else 0)
        prefix_speed[i + 1] = prefix_speed[i] + (speed[i] if valid[i] else 0.0)
        prefix_hr[i + 1] = prefix_hr[i] + (hr[i] if valid[i] else 0.0)

    best_speed = -1.0
    best_result: dict | None = None

    for start in range(n - win + 1):
        end = start + win
        vc = prefix_valid[end] - prefix_valid[start]

        # No valid samples at all → skip immediately.
        if vc == 0:
            continue

        # Check max consecutive-None streak inside the window.  Scan the
        # ``valid`` slice; the window length is bounded by the caller's
        # sample count and is typically ≤ 1200.
        max_gap_here = 0
        streak = 0
        for j in range(start, end):
            if valid[j]:
                streak = 0
            else:
                streak += 1
                if streak > max_gap_here:
                    max_gap_here = streak
                    if max_gap_here > max_gap:
                        break

        if max_gap_here > max_gap:
            continue

        # Window is valid — compute means over valid samples only.
        seg_speed_sum = prefix_speed[end] - prefix_speed[start]
        seg_hr_sum = prefix_hr[end] - prefix_hr[start]
        mean_s = seg_speed_sum / vc
        mean_h = seg_hr_sum / vc

        if mean_s > best_speed:
            best_speed = mean_s
            best_result = {
                "mean_speed": mean_s,
                "mean_hr": mean_h,
                "start_idx": start,
            }

    return best_result


def best_effort_lthr(
    activities: list[Activity],
    series_by_id: dict[int, tuple[list[float | None], list[float | None]]],
    window_s: int,
    factor: float,
) -> dict | None:
    """Find the single best sustained window across all qualifying outdoor-running activities.

    Qualifying filters:
    - ``a.sport == "running"`` (outdoor only — no treadmill, cross, strength)
    - ``a.duration_s >= window_s``
    - ``a.avg_hr is not None``
    - ``a.activity_id in series_by_id``

    For each qualifying activity, ``best_window`` is called on its HR/speed
    series.  The activity with the highest ``mean_speed`` wins.

    Returns:
        ``{proxy_hr, raw_hr, pace, date, activity_id, window_s, factor}`` for
        the best effort, or ``None`` if no qualifying activity has a valid
        window.
    """
    best_speed = -1.0
    best_result: dict | None = None

    for a in activities:
        if a.sport != "running":
            continue
        if a.duration_s < window_s:
            continue
        if a.avg_hr is None:
            continue
        if a.activity_id not in series_by_id:
            continue

        hr_series, speed_series = series_by_id[a.activity_id]
        bw = best_window(hr_series, speed_series, window_s)
        if bw is None:
            continue

        if bw["mean_speed"] > best_speed:
            best_speed = bw["mean_speed"]
            best_result = {
                "proxy_hr": bw["mean_hr"] * factor,
                "raw_hr": bw["mean_hr"],
                "pace": 1.0 / bw["mean_speed"],
                "date": a.date,
                "activity_id": a.activity_id,
                "window_s": window_s,
                "factor": factor,
            }

    return best_result


def best_effort_anchors(
    activities: list[Activity],
    series_by_id: dict[int, tuple[list[float | None], list[float | None]]],
    factor_20: float = 0.95,
    factor_30: float = 0.97,
) -> dict:
    """Compute best-effort LTHR anchors for 20-min and 30-min windows.

    Returns:
        ``{"w20": ..., "w30": ..., "dots20": [...], "dots30": [...]}`` where
        each anchor is the result of :func:`best_effort_lthr` (or ``None``) and
        each dot is ``{hr, pace, date, activity_id}`` for every qualifying
        activity at that window length.
    """
    dots20: list[dict] = []
    dots30: list[dict] = []

    for a in activities:
        if a.sport != "running":
            continue
        if a.avg_hr is None:
            continue
        if a.activity_id not in series_by_id:
            continue

        hr_series, speed_series = series_by_id[a.activity_id]

        if a.duration_s >= 1200:
            bw = best_window(hr_series, speed_series, 1200)
            if bw is not None:
                dots20.append(
                    {
                        "hr": bw["mean_hr"],
                        "pace": 1.0 / bw["mean_speed"],
                        "date": a.date,
                        "activity_id": a.activity_id,
                    }
                )

        if a.duration_s >= 1800:
            bw = best_window(hr_series, speed_series, 1800)
            if bw is not None:
                dots30.append(
                    {
                        "hr": bw["mean_hr"],
                        "pace": 1.0 / bw["mean_speed"],
                        "date": a.date,
                        "activity_id": a.activity_id,
                    }
                )

    w20 = best_effort_lthr(activities, series_by_id, 1200, factor_20)
    w30 = best_effort_lthr(activities, series_by_id, 1800, factor_30)

    return {"w20": w20, "w30": w30, "dots20": dots20, "dots30": dots30}


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
