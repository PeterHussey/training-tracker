"""Raw Garmin JSON -> validated Activity model. Pure: no network."""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

RUNNING_KEYS = {"running"}
TREADMILL_KEYS = {"treadmill_running"}
STRENGTH_KEYS = {"strength_training"}


WORKOUT_CODE_RE = re.compile(r"-\s*([A-Z]+\d{1,2})\s*\(")

# Watch-side feel enum: raw value -> label. Unknown values render as
# f"{raw}?" downstream instead of raising (see parse_rpe_feel).
FEEL_LABELS = {
    0: "Very Weak",
    25: "Weak",
    50: "Normal",
    75: "Strong",
    100: "Very Strong",
}


def workout_code(name: str | None) -> str | None:
    """Extract the 80/20 plan workout code (e.g. 'RF24') from an activity name.

    Matches the code between '<location> - ' and ' (' in Garmin workout names
    like 'Winnetka - RF24 (Foundation Run)'. Returns None for unlabeled
    activities (e.g. 'Treadmill Running').
    """
    if not name:
        return None
    m = WORKOUT_CODE_RE.search(name)
    return m.group(1) if m else None


def sport_of(summary: dict) -> str:
    tk = (summary.get("activityType") or {}).get("typeKey", "")
    if tk in RUNNING_KEYS:
        return "running"
    if tk in TREADMILL_KEYS:
        return "treadmill"
    if tk in STRENGTH_KEYS:
        return "strength"
    return "cross"


def event_type_of(summary: dict) -> str | None:
    """Garmin activity event type (`eventType.typeKey`), or None when absent.

    Carries the raw Garmin key through unchanged (e.g. "uncategorized",
    "race") — it is the only race marker the payload provides, and race
    anchoring keys on it downstream. Older cached payloads and fixtures
    predating the field, malformed shapes, and empty keys all yield None
    rather than a sentinel string.
    """
    et = summary.get("eventType")
    tk = et.get("typeKey") if isinstance(et, dict) else None
    return tk if isinstance(tk, str) and tk else None


def _f(summary: dict, key: str) -> Any:
    v = summary.get(key)
    return v if v is not None else None


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) else None


def parse_rpe_feel(summary: dict | None) -> dict:
    """Watch-side RPE/Feel from a full activity payload (summaryDTO).

    Returns the canonical downstream keys `{rpe_10, feel}`: RPE is Garmin's
    0-100 `directWorkoutRpe` divided to the 0-10 display scale, Feel is a
    FEEL_LABELS label (unknown raw values become f"{raw}?", never raise).
    Missing/malformed payload or fields yield None.
    """
    dto = summary.get("summaryDTO") if isinstance(summary, dict) else None
    if not isinstance(dto, dict):
        dto = {}
    rpe = _num(dto.get("directWorkoutRpe"))
    feel_raw = dto.get("directWorkoutFeel")
    feel = None
    if feel_raw is not None:
        try:
            label = FEEL_LABELS[feel_raw]
        except (KeyError, TypeError):  # unknown value or unhashable — never raise
            label = None
        feel = label if label is not None else f"{feel_raw}?"
    return {"rpe_10": rpe / 10.0 if rpe is not None else None, "feel": feel}


def parse_weather(weather: dict | None) -> dict:
    """Flat canonical weather dict from the activity-weather payload.

    Returns `{wx_temp, wx_feels, wx_humidity, wx_wind, wx_wind_dir, wx_desc,
    wx_unit}`. Values pass through as returned by Garmin (no unit conversion;
    `wx_unit` records the assumed unit, default "F"). Missing/malformed
    payload or fields yield None for every value.
    """
    wx = weather if isinstance(weather, dict) else {}
    wtype = wx.get("weatherTypeDTO")
    desc = wtype.get("desc") if isinstance(wtype, dict) else None
    compass = wx.get("windDirectionCompassPoint")
    degrees = _num(wx.get("windDirection"))
    if isinstance(compass, str) and compass:
        wind_dir: str | None = compass
    elif degrees is not None:
        wind_dir = str(degrees)
    else:
        wind_dir = None
    return {
        "wx_temp": _num(wx.get("temp")),
        "wx_feels": _num(wx.get("apparentTemp")),
        "wx_humidity": _num(wx.get("relativeHumidity")),
        "wx_wind": _num(wx.get("windSpeed")),
        "wx_wind_dir": wind_dir,
        "wx_desc": desc if isinstance(desc, str) and desc else None,
        "wx_unit": "F",
    }


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
    has_intervals: bool = False
    device_id: int | None = None
    location: str | None = None
    lat: float | None = None
    lon: float | None = None
    name: str | None = None
    # Garmin `eventType.typeKey`, e.g. "uncategorized" / "race"; None for
    # payloads that predate the field. Race anchoring reads this downstream.
    event_type: str | None = None
    # RPE / Feel / weather — backfilled from the summary & weather endpoints
    # (parse_rpe_feel / parse_weather); None until that backfill runs.
    rpe_10: float | None = None
    feel: str | None = None
    wx_temp: float | None = None
    wx_feels: float | None = None
    wx_humidity: float | None = None
    wx_wind: float | None = None
    wx_wind_dir: str | None = None
    wx_desc: str | None = None
    wx_unit: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def distance_km(self) -> float:
        return round(self.distance_m / 1000.0, 6)

    @property
    def duration_min(self) -> float:
        return round(self.duration_s / 60.0, 6)

    @property
    def code(self) -> str | None:
        """80/20 plan workout code from `name`, or None when not labeled."""
        return workout_code(self.name)


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
        has_intervals=bool(summary.get("hasIntensityIntervals", False)),
        device_id=_f(summary, "deviceId"),
        location=_f(summary, "locationName"),
        lat=_f(summary, "startLatitude"),
        lon=_f(summary, "startLongitude"),
        name=_f(summary, "activityName"),
        event_type=event_type_of(summary),
        raw=summary,
    )
