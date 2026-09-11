"""Aerobic decoupling (HR vs pace drift over a sustained effort).

Research brief 3.1: real signal, but only honest when aggregated over >=6
sessions on similar flat routes and presented as a trend. Single-run values
are dominated by day-to-day noise (session-residual variance 57-83%).
"""

import statistics
from collections.abc import Sequence

from normalize import Activity


def decoupling_percent(hr: list[float], speed: list[float]) -> float:
    n = len(hr)
    if n < 2 or len(speed) != n:
        raise ValueError("need equal hr/speed sequences with >=2 samples")
    half = n // 2
    first_hr, first_sp = hr[:half], speed[:half]
    second_hr, second_sp = hr[half:], speed[half:]

    def ratio(hrs, sps):
        mean_sp = sum(sps) / len(sps)
        if mean_sp == 0:
            raise ValueError("zero average speed in a half")
        return (sum(hrs) / len(hrs)) / mean_sp

    r1 = ratio(first_hr, first_sp)
    r2 = ratio(second_hr, second_sp)
    return (r2 / r1) - 1.0


def decoupling_timebased(
    hr: Sequence[float | None], speed: Sequence[float | None], ts_ms: Sequence[float | None]
) -> float:
    """Decoupling split by elapsed-time midpoint (for irregularly sampled series).

    Live samples arrive ~1-7 s apart, so a sample-count split can misalign
    the halves in time. Drops samples with missing HR/speed/timestamp, splits
    the valid samples at the elapsed midpoint, and applies the same HR/speed
    ratio comparison as :func:`decoupling_percent`.
    """
    if not (len(hr) == len(speed) == len(ts_ms)):
        raise ValueError("need equal hr/speed/ts sequences")
    valid = [
        (h, s, t)
        for h, s, t in zip(hr, speed, ts_ms, strict=True)
        if h is not None and s is not None and t is not None
    ]
    if len(valid) < 2:
        raise ValueError("need >=2 valid hr/speed samples")
    lo = min(t for _, _, t in valid)
    mid = lo + (max(t for _, _, t in valid) - lo) / 2.0
    first = [(h, s) for h, s, t in valid if t <= mid]
    second = [(h, s) for h, s, t in valid if t > mid]
    if not first or not second:
        raise ValueError("samples do not span the elapsed midpoint")

    def ratio(pairs):
        mean_sp = sum(s for _, s in pairs) / len(pairs)
        if mean_sp == 0:
            raise ValueError("zero average speed in a half")
        return (sum(h for h, _ in pairs) / len(pairs)) / mean_sp

    return ratio(second) / ratio(first) - 1.0


def eligible_activity(
    a: Activity,
    min_duration_s: int = 5400,
    max_ele_per_km: float = 25.0,
    exclude_intervals: bool = True,
) -> bool:
    if a.sport != "running":
        return False
    if exclude_intervals and a.has_intervals:
        return False
    if a.elapsed_s < min_duration_s:
        return False
    return not (
        a.ele_gain_m is not None
        and a.distance_m > 0
        and a.ele_gain_m / (a.distance_m / 1000.0) > max_ele_per_km
    )


def route_key(a: Activity, grid: float = 0.01):
    if a.lat is None or a.lon is None:
        return ("unknown", None)
    return (round(a.lat / grid) * grid, round(a.lon / grid) * grid)


def aggregate_decoupling(decouplings: list[float], min_sessions: int = 6):
    if len(decouplings) < min_sessions:
        return None
    return {
        "n": len(decouplings),
        "mean": statistics.mean(decouplings),
        "stdev": statistics.stdev(decouplings) if len(decouplings) > 1 else 0.0,
        "min_sessions_applied": min_sessions,
    }
