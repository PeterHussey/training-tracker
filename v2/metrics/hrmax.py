"""Estimated HRmax from observed workout HR (falls back to age prediction).

A single high reading can be a monitor artifact, so the estimator uses the
highest max-HR that recurs on >= `min_recurrence` distinct calendar days and
clears a plausibility floor. One-off spikes (e.g. a genuine single max effort)
are intentionally discounted — for those, set HRmax manually
(hrmax_source="configured").
"""

from collections import defaultdict

from normalize import Activity


def estimate_hrmax(
    activities: list[Activity], min_recurrence: int = 2, floor: int = 120
) -> int | None:
    """Highest recurring observed max HR, or None when data is insufficient."""
    seen_by_value: dict[int, set] = defaultdict(set)
    for a in activities:
        if a.max_hr is None:
            continue
        value = int(round(float(a.max_hr)))
        if value < floor:
            continue
        seen_by_value[value].add(a.date)
    recurring = [v for v, days in seen_by_value.items() if len(days) >= min_recurrence]
    return max(recurring) if recurring else None
