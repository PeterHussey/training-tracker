"""Real metric calculations from Garmin cached JSON."""
import json
from pathlib import Path
from datetime import datetime

CACHE_DIR = Path("cache")
ZONE_WEIGHTS = {1: 1.0, 2: 2.0, 3: 2.5, 4: 3.5, 5: 4.5}


def load_raw(date_tag=None):
    path = CACHE_DIR / "garmin_raw.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def parse_activities_from_raw(raw):
    """Extract activity list from Garmin response (handles multiple shapes)."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        # Common shapes: {"activities": [...]} or {"result":{"activities":[...]}}
        if "activities" in raw:
            return raw["activities"]
        if "result" in raw and isinstance(raw["result"], dict) and "activities" in raw["result"]:
            return raw["result"]["activities"]
        if "get_activities_result" in raw:
            inner = raw["get_activities_result"]
            if isinstance(inner, dict) and "activities" in inner:
                return inner["activities"]
            if isinstance(inner, list):
                return inner
    return []


def daily_trimp_from_activities(raw) -> list:
    """Compute approximate daily TRIMP from cached Garmin activities."""
    acts = parse_activities_from_raw(raw)
    # Aggregate by date (startTimeLocal YYYY-MM-DD)
    from collections import defaultdict
    daily = defaultdict(float)
    for a in acts:
        date_str = a.get("startTimeLocal", "")[:10] if isinstance(a.get("startTimeLocal"), str) else "unknown"
        if not date_str or date_str == "unknown":
            continue
        # Duration in minutes; approximate zone from avgHR
        duration = a.get("duration", 0) / 60.0  # assume seconds
        avg_hr = a.get("averageHeartRate") or a.get("avgHR") or 130
        # Simplified zone mapping
        zone = 2 if avg_hr < 145 else 3 if avg_hr < 165 else 4 if avg_hr < 180 else 5
        trimp = duration * ZONE_WEIGHTS.get(zone, 2.0)
        daily[date_str] += trimp
    # Return sorted list of 30 days of values (zeros for missing days)
    return sorted(daily.values())[-30:] if daily else [0.0] * 30


def compute_trimp(zones: dict) -> float:
    return sum(minutes * ZONE_WEIGHTS.get(z, 1.0) for z, minutes in zones.items())


def acwr(seven_day_trimp: float, twenty_eight_day_trimp: float) -> float:
    return 0.0 if twenty_eight_day_trimp == 0 else seven_day_trimp / twenty_eight_day_trimp


def banister_ctl_atl_tsb(daily_trimp: list, days: int = 30) -> tuple:
    ctl_series, atl_series = [], []
    ctl, atl = 0.0, 0.0
    for trimp in daily_trimp:
        ctl = trimp if ctl == 0 else ctl + (trimp - ctl) / 42
        atl = trimp if atl == 0 else atl + (trimp - atl) / 7
        ctl_series.append(ctl)
        atl_series.append(atl)
    tsb = [c - a for c, a in zip(ctl_series, atl_series)]
    return ctl_series[-1] if ctl_series else 0.0, atl_series[-1] if atl_series else 0.0, tsb[-1] if tsb else 0.0, ctl_series, atl_series, tsb


def synthetic_trimp(days=30):
    return [120.0] * days
