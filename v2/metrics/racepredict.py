"""Garmin race predictions — ingested reference (research brief 2.3).

Riegel-derived. 5K/10K/half are the trustworthy end; the marathon prediction
is the least trustworthy number Garmin produces (underestimates by >=10 min
for ~half of runners). Stored as a dated series and trended, never prescribed.
"""

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
