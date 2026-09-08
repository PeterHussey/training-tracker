"""Aerobic decoupling (HR vs pace drift over a sustained effort).

Research brief 3.1: real signal, but only honest when aggregated over >=6
sessions on similar flat routes and presented as a trend. Single-run values
are dominated by day-to-day noise (session-residual variance 57-83%).
"""

import statistics

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


def eligible_activity(
    a: Activity, min_duration_s: int = 5400, max_ele_per_km: float = 25.0
) -> bool:
    if a.sport != "running":
        return False
    if a.elapsed_s < min_duration_s:
        return False
    return not (a.ele_gain_m is not None and a.distance_m > 0 and a.ele_gain_m / (a.distance_m / 1000.0) > max_ele_per_km)


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
