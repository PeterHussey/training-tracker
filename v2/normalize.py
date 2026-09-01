"""Raw Garmin JSON -> validated Activity model. Pure: no network."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

RUNNING_KEYS = {"running"}
TREADMILL_KEYS = {"treadmill_running"}


def sport_of(summary: dict) -> str:
    tk = (summary.get("activityType") or {}).get("typeKey", "")
    if tk in RUNNING_KEYS:
        return "running"
    if tk in TREADMILL_KEYS:
        return "treadmill"
    return "cross"


def _f(summary: dict, key: str) -> Any:
    v = summary.get(key)
    return v if v is not None else None


@dataclass
class Activity:
    activity_id: int
    sport: str
    date: date
    ts_ms: int
    distance_m: float
    duration_s: float
    elapsed_s: float
    avg_hr: float | None
    max_hr: float | None
    zone_s: dict[int, float] = field(default_factory=dict)
    ele_gain_m: float | None = None
    ele_loss_m: float | None = None
    vo2max: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    avg_speed: float | None = None
    fastest_split_1609: float | None = None
    avg_cadence: float | None = None
    device_id: int | None = None
    location: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def distance_km(self) -> float:
        return round(self.distance_m / 1000.0, 6)

    @property
    def duration_min(self) -> float:
        return round(self.duration_s / 60.0, 6)


def from_summary(summary: dict) -> Activity:
    ts_local = str(_f(summary, "startTimeLocal") or "")[:19]
    if ts_local:
        d = datetime.strptime(ts_local, "%Y-%m-%d %H:%M:%S").date()
    else:
        ts_ms = _f(summary, "beginTimestamp")
        d = (
            datetime.fromtimestamp(ts_ms / 1000.0).date()
            if isinstance(ts_ms, (int, float))
            else date.today()
        )
    zone_s = {z: float(_f(summary, f"hrTimeInZone_{z}") or 0.0) for z in range(1, 6)}
    return Activity(
        activity_id=int(summary.get("activityId")),
        sport=sport_of(summary),
        date=d,
        ts_ms=int(_f(summary, "beginTimestamp") or 0),
        distance_m=float(_f(summary, "distance") or 0.0),
        duration_s=float(_f(summary, "duration") or 0.0),
        elapsed_s=float(_f(summary, "elapsedDuration") or 0.0),
        avg_hr=_f(summary, "averageHR"),
        max_hr=_f(summary, "maxHR"),
        zone_s=zone_s,
        ele_gain_m=_f(summary, "elevationGain"),
        ele_loss_m=_f(summary, "elevationLoss"),
        vo2max=_f(summary, "vO2MaxValue"),
        aerobic_te=_f(summary, "aerobicTrainingEffect"),
        anaerobic_te=_f(summary, "anaerobicTrainingEffect"),
        avg_speed=_f(summary, "averageSpeed"),
        fastest_split_1609=_f(summary, "fastestSplit_1609"),
        avg_cadence=_f(summary, "averageRunningCadenceInStepsPerMinute"),
        device_id=_f(summary, "deviceId"),
        location=_f(summary, "locationName"),
        lat=_f(summary, "startLatitude"),
        lon=_f(summary, "startLongitude"),
        raw=summary,
    )
