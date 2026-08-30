"""HR-based training load: Edwards TRIMP (zone seconds) + Banister TRIMP (dHR exponential).

Research brief 1.2: Banister is the primary load measure (physiologically grounded);
Edwards is kept as a cheap, reproducible cross-check. Absolute values are only
comparable within an individual over time with identical profile parameters.
"""
import pandas as pd

from normalize import Activity
from profile import RunnerProfile

E = 2.718281828459045


def edwards_trimp(zone_s: dict[int, float], weights: dict[int, float] | None = None) -> float:
    w = weights or RunnerProfile.EDWARDS_WEIGHTS  # type: ignore[attr-defined]
    return float(sum((zone_s.get(z, 0.0) / 60.0) * w[z] for z in w))


def banister_trimp(duration_min: float, avg_hr: float, profile: RunnerProfile) -> float:
    hrmax = float(profile.hrmax)
    hrrest = float(profile.hrrest)
    if hrmax <= hrrest:
        raise ValueError("hrmax must exceed hrrest")
    dhr = (avg_hr - hrrest) / (hrmax - hrrest)
    dhr = min(max(dhr, 0.0), 1.0)
    return duration_min * dhr * profile.exp_intercept_factor() * (E ** (profile.banister_exponent() * dhr))


def daily_trimp(activities: list[Activity], profile: RunnerProfile) -> pd.DataFrame:
    rows = []
    for a in activities:
        if a.avg_hr is None or a.duration_s <= 0:
            continue
        dur_min = a.duration_s / 60.0
        rows.append({
            "date": a.date,
            "banister": banister_trimp(dur_min, float(a.avg_hr), profile),
            "edwards": edwards_trimp(a.zone_s),
        })
    if not rows:
        return pd.DataFrame(columns=["date", "banister", "edwards"])
    df = pd.DataFrame(rows)
    agg = df.groupby("date", as_index=False)[["banister", "edwards"]].sum()
    agg["date"] = pd.to_datetime(agg["date"])
    return agg.set_index("date")