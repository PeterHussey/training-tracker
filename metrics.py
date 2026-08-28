"""Training metrics calculations from Garmin cached data."""
import json
from pathlib import Path
from datetime import datetime

CACHE_DIR = Path("cache")

ZONE_WEIGHTS = {1: 1.0, 2: 2.0, 3: 2.5, 4: 3.5, 5: 4.5}


def load_raw(date_tag=None):
    files = list(CACHE_DIR.glob("garmin_raw_*.json"))
    if not files:
        return {}
    # Pick latest by filename timestamp or just first
    return json.loads(files[0].read_text())


def compute_trimp(zones: dict) -> float:
    trimp = 0.0
    for z, minutes in zones.items():
        trimp += minutes * ZONE_WEIGHTS.get(z, 1.0)
    return trimp


def acwr(seven_day_trimp: float, twenty_eight_day_trimp: float) -> float:
    if twenty_eight_day_trimp == 0:
        return 0.0
    return seven_day_trimp / twenty_eight_day_trimp


def banister_ctl_atl_tsb(daily_trimp: list, days: int = 30) -> tuple:
    ctl = 0.0
    atl = 0.0
    for trimp in daily_trimp[-days:]:
        ctl = ctl + (trimp - ctl) / 42 if ctl else trimp
        atl = atl + (trimp - atl) / 7 if atl else trimp
    # Initialize on first pass: use first value as seed
    # Simplified rolling: return final values
    # Re-running properly for series
    ctl_series = []
    atl_series = []
    ctl, atl = 0.0, 0.0
    for trimp in daily_trimp:
        if ctl == 0:
            ctl = trimp
        else:
            ctl = ctl + (trimp - ctl) / 42
        if atl == 0:
            atl = trimp
        else:
            atl = atl + (trimp - atl) / 7
        ctl_series.append(ctl)
        atl_series.append(atl)
    tsb = [c - a for c, a in zip(ctl_series, atl_series)]
    return ctl_series[-1], atl_series[-1], tsb[-1], ctl_series, atl_series, tsb


def synthetic_trimp(days=14, base=60, hr=150):
    # Synthetic verification data: 60 min easy runs at ~150 bpm
    # Approx zone 2 weight ~2.0 => TRIMP ~ 60*2 = 120/day
    return [120.0] * days

if __name__ == "__main__":
    # Synthetic verification
    trimp_series = synthetic_trimp(14)
    acwr_val = acwr(sum(trimp_series[-7:]), sum(trimp_series[-28:] if len(trimp_series) >= 28 else trimp_series))
    ctl, atl, tsb, _, _, _ = banister_ctl_atl_tsb(trimp_series)
    print(f"Synthetic TRIMP/day={trimp_series[0]} ACWR={acwr_val:.2f} CTL={ctl:.1f} ATL={atl:.1f} TSB={tsb:.1f}")
