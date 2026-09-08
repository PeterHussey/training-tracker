"""Garmin race predictions — ingested reference (research brief 2.3).

Riegel-derived. 5K/10K/half are the trustworthy end; the marathon prediction
is the least trustworthy number Garmin produces (underestimates by >=10 min
for ~half of runners). Stored as a dated series and trended, never prescribed.
"""

import pandas as pd

DISTANCE_KEYS = {
    "5k": "Run_5k",
    "10k": "Run_10k",
    "half": "Run_half_marathon",
    "full": "Run_full_marathon",
}

LIVE_KEYS = {
    "5k": "time5K",
    "10k": "time10K",
    "half": "timeHalfMarathon",
    "full": "timeMarathon",
}


def parse_predictions(payload: dict) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    if not isinstance(payload, dict):
        return dict.fromkeys(DISTANCE_KEYS)
    canonical = any(k in payload for k in DISTANCE_KEYS.values())
    live = any(k in payload for k in LIVE_KEYS.values())
    for dist, key in DISTANCE_KEYS.items():
        raw = None
        if canonical:
            entry = payload.get(key)
            if isinstance(entry, dict):
                raw = entry.get("time") or entry.get("goalTime")
        elif live:
            raw = payload.get(LIVE_KEYS[dist])
        if raw is None:
            out[dist] = None
            continue
        ms = int(raw)
        out[dist] = round(ms / 1000.0) if canonical else ms
    return out


def daily_predictions_from_trend(trend_payload: list[dict]) -> dict[str, pd.Series]:
    """Build per-distance daily series from the /racepredictions/daily endpoint.

    Each entry carries one day's snapshot in the flat live schema (time5K +
    calendarDate). Days missing a distance stay missing for that distance —
    no forward-fill, so gaps in Garmin's history show as gaps. Duplicate
    dates keep the last entry.
    """
    rows: dict[str, list[tuple]] = {dist: [] for dist in DISTANCE_KEYS}
    for entry in trend_payload or []:
        if not isinstance(entry, dict):
            continue
        date_str = entry.get("calendarDate")
        if not date_str:
            continue
        ts = pd.Timestamp(date_str)
        for dist, key in LIVE_KEYS.items():
            raw = entry.get(key)
            if raw is None:
                continue
            rows[dist].append((ts, float(int(raw))))
    out = {}
    for dist, pairs in rows.items():
        if not pairs:
            out[dist] = pd.Series([], dtype=float)
            continue
        s = pd.Series(
            [v for _, v in pairs],
            index=pd.DatetimeIndex([d for d, _ in pairs]),
        )
        out[dist] = s.groupby(s.index).last().sort_index()
    return out
