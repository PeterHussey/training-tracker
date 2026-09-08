"""Lactate threshold (HR anchored) + approximate critical speed.

Research brief 2.2: LT heart rate is the trustworthy anchor (~7% error); LT
pace can overestimate 20-26%. Critical speed is approximated here from the
fastest 1-mile split per session — a trend signal, NOT a lab-derived CS.
"""

from dataclasses import dataclass

import pandas as pd

from normalize import Activity


def parse_details_series(
    details: dict,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    """Extract aligned HR, speed, and timestamp lists from a Garmin details payload.

    Live shape (verified against cached ``activity_details_{id}.json``):
    ``metricDescriptors`` maps column index -> metric key
    (``directHeartRate`` in bpm, ``directSpeed`` in m/s, ``directTimestamp``
    in epoch ms); ``activityDetailMetrics`` holds one ``{metrics: [...]}``
    row per sample. Values arrive pre-scaled (sumDistance matches the
    activity-list distance exactly), so descriptor ``factor`` metadata is
    NOT applied. Samples are irregularly spaced (~1-7 s apart), hence the
    returned timestamps.

    Args:
        details: Raw details dict as returned by
            ``GET /activity-service/activity/{id}/details``.

    Returns:
        ``(hr, speed, ts_ms)`` — three lists of equal length. ``None``
        values represent optical HR dropouts or GPS gaps. Returns
        ``([], [], [])`` when any of the three descriptors is absent.
    """
    descriptors = details.get("metricDescriptors") or []
    index_of = {md.get("key"): md.get("metricsIndex") for md in descriptors}
    hr_idx = index_of.get("directHeartRate")
    speed_idx = index_of.get("directSpeed")
    ts_idx = index_of.get("directTimestamp")
    if hr_idx is None or speed_idx is None or ts_idx is None:
        return [], [], []
    hr: list[float | None] = []
    speed: list[float | None] = []
    ts_ms: list[float | None] = []
    for entry in details.get("activityDetailMetrics") or []:
        row = entry.get("metrics") or []
        hr.append(row[hr_idx] if hr_idx < len(row) else None)
        speed.append(row[speed_idx] if speed_idx < len(row) else None)
        ts_ms.append(row[ts_idx] if ts_idx < len(row) else None)
    return hr, speed, ts_ms


def best_window(
    hr: list[float | None],
    speed: list[float | None],
    window_s: int,
    sample_s: float = 1.0,
    max_gap_s: float = 5.0,
    ts_ms: list[float | None] | None = None,
) -> dict | None:
    """Find the fastest contiguous window of a given duration.

    Two modes: with ``ts_ms`` (per-sample epoch-ms timestamps, as returned by
    :func:`parse_details_series` for irregularly sampled live data) the
    window is a true time interval; without it, samples are assumed uniform
    at ``sample_s`` seconds apart (legacy path, kept for synthetic tests).

    Gaps up to ``max_gap_s`` seconds are tolerated; only valid (non-``None``)
    samples feed the means.  A window is valid when its valid samples span
    at least ``window_s - max_gap_s`` seconds with no interior hole (time
    between consecutive valid samples) exceeding ``max_gap_s``.

    Args:
        hr: Per-sample heart rate (may contain None for dropouts).
        speed: Per-sample speed in m/s (may contain None for gaps).
        window_s: Desired window duration in seconds.
        sample_s: Seconds per sample for the uniform path (default 1 Hz).
        max_gap_s: Maximum tolerated gap in seconds.
        ts_ms: Per-sample timestamps in epoch ms, or None for uniform spacing.

    Returns:
        ``{mean_speed, mean_hr, start_idx}`` for the fastest valid window, or
        ``None`` if no valid window exists.
    """
    n = len(hr)
    if n != len(speed) or n == 0 or window_s <= 0:
        return None
    if ts_ms is not None:
        return _best_window_time(hr, speed, ts_ms, window_s, max_gap_s)

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


def _best_window_time(
    hr: list[float | None],
    speed: list[float | None],
    ts_ms: list[float | None],
    window_s: int,
    max_gap_s: float,
) -> dict | None:
    """Time-domain sliding window for irregularly sampled series (live data).

    Samples arrive ~1-7 s apart, so a fixed sample count is NOT a fixed
    duration.  Each candidate window is the time interval
    ``[ts[i], ts[i] + window_s]``; means are plain averages over its valid
    samples.  A gap is time between consecutive *valid* samples, which
    covers both missing rows and ``None`` values     (e.g. optical HR dropout
    while GPS continues).
    """
    n = len(hr)
    if len(ts_ms) != n:
        return None
    # Drop timestamp-less samples up front; they carry no time information.
    ts: list[float] = []
    valid: list[bool] = []
    spd: list[float] = []
    bpm: list[float] = []
    for h, s, t in zip(hr, speed, ts_ms, strict=True):
        if t is None:
            continue
        ts.append(t)
        ok = h is not None and s is not None
        valid.append(ok)
        spd.append(s if ok else 0.0)
        bpm.append(h if ok else 0.0)
    m = len(ts)
    if m == 0:
        return None

    win_ms = window_s * 1000.0
    # Live series are sparsely sampled (~1-9 s apart, sparser on long
    # activities capped by maxChartSize), so a fixed gap tolerance
    # calibrated for 1 Hz data would reject healthy windows. Adapt: holes
    # up to 3x the median sampling interval are sampling artifacts, not
    # dropouts. A tolerated hole barely moves a 20-30 min mean (<2%).
    import statistics

    diffs = [b - a for a, b in zip(ts, ts[1:], strict=False) if b > a]
    median_step_ms = statistics.median(diffs) if diffs else 1000.0
    max_gap_ms = max(max_gap_s * 1000.0, 3.0 * median_step_ms)
    min_span_ms = win_ms - max_gap_ms

    prefix_valid = [0] * (m + 1)
    prefix_speed = [0.0] * (m + 1)
    prefix_hr = [0.0] * (m + 1)
    for i in range(m):
        prefix_valid[i + 1] = prefix_valid[i] + (1 if valid[i] else 0)
        prefix_speed[i + 1] = prefix_speed[i] + spd[i]
        prefix_hr[i + 1] = prefix_hr[i] + bpm[i]

    best_speed = -1.0
    best_result: dict | None = None
    j = 0
    for i in range(m):
        if j < i + 1:
            j = i + 1
        end_t = ts[i] + win_ms
        while j < m and ts[j] <= end_t:
            j += 1
        # Window samples are [i, j); need at least one valid sample.
        if prefix_valid[j] - prefix_valid[i] == 0:
            continue
        # Coverage: valid samples must span nearly the whole window.
        first = next(k for k in range(i, j) if valid[k])
        last = next(k for k in range(j - 1, i - 1, -1) if valid[k])
        if ts[last] - ts[first] < min_span_ms:
            continue
        # Gaps: no hole between consecutive valid samples exceeds tolerance.
        prev_t = None
        hole_ok = True
        for k in range(i, j):
            if not valid[k]:
                continue
            if prev_t is not None and ts[k] - prev_t > max_gap_ms:
                hole_ok = False
                break
            prev_t = ts[k]
        if not hole_ok:
            continue
        vc = prefix_valid[j] - prefix_valid[i]
        mean_s = (prefix_speed[j] - prefix_speed[i]) / vc
        mean_h = (prefix_hr[j] - prefix_hr[i]) / vc
        if mean_s > best_speed:
            best_speed = mean_s
            best_result = {
                "mean_speed": mean_s,
                "mean_hr": mean_h,
                "start_idx": i,
            }
    return best_result


Series = tuple[list[float | None], list[float | None], list[float | None]]


def best_effort_lthr(
    activities: list[Activity],
    series_by_id: dict[int, Series],
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
        ``{proxy_hr, raw_hr, speed_m_s, date, activity_id, window_s, factor}``
        for the best effort, or ``None`` if no qualifying activity has a
        valid window. Pace is stored as speed (m/s) to match the Garmin
        ``load.lt_pace`` convention and the ``unit: m/s`` row params.
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

        hr_series, speed_series, ts_series = series_by_id[a.activity_id]
        bw = best_window(hr_series, speed_series, window_s, ts_ms=ts_series)
        if bw is None:
            continue

        if bw["mean_speed"] > best_speed:
            best_speed = bw["mean_speed"]
            best_result = {
                "proxy_hr": bw["mean_hr"] * factor,
                "raw_hr": bw["mean_hr"],
                "speed_m_s": bw["mean_speed"],
                "date": a.date,
                "activity_id": a.activity_id,
                "window_s": window_s,
                "factor": factor,
            }

    return best_result


def best_effort_anchors(
    activities: list[Activity],
    series_by_id: dict[int, Series],
    factor_20: float = 0.95,
    factor_30: float = 0.97,
) -> dict:
    """Compute best-effort LTHR anchors for 20-min and 30-min windows.

    Returns:
        ``{"w20": ..., "w30": ..., "dots20": [...], "dots30": [...]}`` where
        each anchor is the result of :func:`best_effort_lthr` (or ``None``) and
        each dot is ``{hr, speed_m_s, date, activity_id}`` for every
        qualifying activity at that window length.
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

        hr_series, speed_series, ts_series = series_by_id[a.activity_id]

        if a.duration_s >= 1200:
            bw = best_window(hr_series, speed_series, 1200, ts_ms=ts_series)
            if bw is not None:
                dots20.append(
                    {
                        "hr": bw["mean_hr"],
                        "speed_m_s": bw["mean_speed"],
                        "date": a.date,
                        "activity_id": a.activity_id,
                    }
                )

        if a.duration_s >= 1800:
            bw = best_window(hr_series, speed_series, 1800, ts_ms=ts_series)
            if bw is not None:
                dots30.append(
                    {
                        "hr": bw["mean_hr"],
                        "speed_m_s": bw["mean_speed"],
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
