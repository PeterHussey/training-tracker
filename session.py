"""Session-view builder for the dashboard.

Pure (no Streamlit, no network): organizes pipeline.compute_metric_rows into
dated series + interpretation context, and owns the date-filtering / unit-
formatting helpers shared with e2e_report.py so the CLI report and the
dashboard use one implementation.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from pipeline import compute_metric_rows

if TYPE_CHECKING:
    from profile import RunnerProfile

WEEK_RE = re.compile(r"(\d{4})-W(\d{1,2})")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KM_PER_MI = 1.609344

EXPECTED_METRICS = [
    "volume.distance_total",
    "volume.duration_total",
    "load.banister",
    "load.edwards",
    "pmc.ctl",
    "pmc.atl",
    "pmc.tsb",
    "fitness.vo2max",
    "load.cs_approx",
    "injury.max_run_ratio",
]

CONDITIONAL_METRICS = {
    "load.acwr": "requires a >=28-day span of HR-load activities (running + treadmill + cross) for the chronic window",
    "load.banister_cross": "requires cross-training activities with HR data",
    "load.edwards_cross": "requires cross-training activities with HR data",
    "load.banister_treadmill": "requires treadmill activities with HR data",
    "load.edwards_treadmill": "requires treadmill activities with HR data",
    "load.banister_running": "requires outdoor-running activities with HR data",
    "load.edwards_running": "requires outdoor-running activities with HR data",
    "volume.duration_cross": "time-volume is populated whenever a cross-training activity exists (duration is distance-independent)",
    "volume.duration_strength": "time-volume is populated whenever a strength activity exists (duration is distance-independent)",
    "gap.running": "requires outdoor-running activities with elevation data",
    "load.lt_hr": "requires a measured lactate threshold in the payload (live LT record was empty)",
    "load.lt_pace": "requires a measured lactate threshold in the payload (live LT record was empty)",
    "load.lt_hr_best20": "best-effort LTHR anchor from 20-min window (outdoor running, Garmin LT absent)",
    "load.lt_pace_best20": "best-effort LT pace anchor from 20-min window (outdoor running, Garmin LT absent)",
    "load.lt_hr_best30": "best-effort LTHR anchor from 30-min window (outdoor running, Garmin LT absent)",
    "load.lt_pace_best30": "best-effort LT pace anchor from 30-min window (outdoor running, Garmin LT absent)",
    "load.lt_effort_dots20_hr": "qualifier HR dots for 20-min best-effort window",
    "load.lt_effort_dots20_pace": "qualifier pace dots for 20-min best-effort window",
    "load.lt_effort_dots30_hr": "qualifier HR dots for 30-min best-effort window",
    "load.lt_effort_dots30_pace": "qualifier pace dots for 30-min best-effort window",
    "race_5k": "requires non-null race predictions in the payload",
    "race_10k": "requires non-null race predictions in the payload",
    "race_half": "requires non-null race predictions in the payload",
    "race_full": "requires non-null race predictions in the payload",
    "load.decoupling": "requires route-matched flat 90min+ outdoor runs with HR/speed details",
    "load.decoupling_mean": "requires >=6 route-matched flat 90min+ sessions (single-run values are noise)",
}


def week_start(label: str, monday: bool = True) -> date | None:
    """ISO-week label -> Monday date. Returns None for non-week labels."""
    m = WEEK_RE.match(label.strip())
    if not m:
        return None
    try:
        return date.fromisocalendar(int(m.group(1)), int(m.group(2)), 1)
    except (ValueError, TypeError):
        return None


def in_period(row_date: str, since: date) -> bool:
    """True when a row's date falls on/after `since`.

    Daily rows compare by calendar date; ISO-week rows are included when their
    week end (Sunday) is >= since.
    """
    if DAY_RE.fullmatch(row_date):
        return date.fromisoformat(row_date) >= since
    ws = week_start(row_date)
    if ws is not None:
        return ws + timedelta(days=6) >= since
    return False


def filter_period(rows: list[dict], since: date) -> list[dict]:
    return [r for r in rows if in_period(str(r["date"]), since)]


def _safe_json(text) -> dict:
    if not text:
        return {}
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _hhmm(value: float) -> str:
    total = int(round(value))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _pace_mi(seconds: float) -> str:
    return f"{_hhmm(seconds)} /mi"


def _fmt(value: float, params: dict, units: str = "km") -> str:
    unit = params.get("unit")
    if units in ("miles", "imperial"):
        if unit == "km":
            return f"{value / KM_PER_MI:.2f} mi"
        if unit == "m":
            return f"{value * 3.28084:.0f} ft"
        if unit == "m/km":
            return f"{value * (3.28084 / (1.0 / KM_PER_MI)):.1f} ft/mi"
        if unit in ("m/s", "m_s") or "m/s" in str(unit):
            if value > 0:
                return _pace_mi(1609.344 / value)
            return "--"
    if unit == "km":
        return f"{value:.2f} km"
    if unit == "m":
        return f"{value:.0f} m"
    if unit == "m/km":
        return f"{value:.1f} m/km"
    if unit in ("m/s", "m_s") or "m/s" in str(unit):
        return f"{value:.2f} m/s"
    if unit == "s":
        return _hhmm(value)
    if unit == "bpm":
        return f"{value:.0f} bpm"
    if unit == "fraction":
        return f"{value * 100:.1f}%"
    return f"{value:.2f}"


def _row_ts(r: dict) -> pd.Timestamp | None:
    label = str(r["date"])
    if DAY_RE.fullmatch(label):
        return pd.Timestamp(label)
    ws = week_start(label)
    return pd.Timestamp(ws) if ws is not None else None


@dataclass
class SessionView:
    """Organized metric rows plus interpretation context for one profile run.

    `series[metric]` is a pd.Series with a sorted DatetimeIndex (weekly rows
    dated at their Monday). `context[metric]` carries {"params", "flags",
    "source"} parsed from the first row of each metric.
    """

    profile: RunnerProfile
    series: dict[str, pd.Series]
    context: dict[str, dict]

    @property
    def metrics(self) -> list[str]:
        return sorted(self.series)

    def windowed(self, since: date, until: date) -> dict[str, pd.Series]:
        """Return series filtered to [since, until].

        Weekly rows (params agg == "iso_week") are kept when their week overlaps
        the window (Monday <= until AND Sunday >= since); daily rows by date.
        """
        out = {}
        for metric, s in self.series.items():
            weekly = self.context[metric]["params"].get("agg") == "iso_week"
            out[metric] = window_series(s, since, until, weekly=weekly)
        return out


def window_series(s: pd.Series, since: date, until: date, weekly: bool = False) -> pd.Series:
    if weekly:
        starts = pd.to_datetime(s.index)
        ends = starts + pd.Timedelta(days=6)
        mask = (starts <= pd.Timestamp(until)) & (ends >= pd.Timestamp(since))
        return s[mask]
    idx = pd.to_datetime(s.index)
    mask = (idx >= pd.Timestamp(since)) & (idx <= pd.Timestamp(until))
    return s[mask]


def build_repetitions(activities) -> dict[str, list]:
    """Group activities by their 80/20 plan workout code, keyed oldest-first.

    Only activities with a recognizable code (e.g. 'RF24' in
    'Winnetka - RF24 (Foundation Run)') are included; unlabeled activities
    (treadmill, strength, indoor cycling) are omitted.
    """
    groups: dict[str, list] = {}
    for a in activities:
        code = a.code
        if code:
            groups.setdefault(code, []).append(a)
    for code in groups:
        groups[code].sort(key=lambda a: (a.date, a.activity_id))
    return groups


def repetition_rows(activities, code: str) -> list[dict]:
    """Per-repetition comparison rows for one workout code, oldest first.

    Each row carries the fields the dashboard plots/musters for a single
    repetition (pace is kept as m/s in avg_speed and distance as km, so the
    view owns unit formatting).
    """
    rows = []
    for a in activities:
        if a.code != code:
            continue
        rows.append(
            {
                "activity_id": a.activity_id,
                "date": a.date.isoformat(),
                "name": a.name,
                "distance_km": a.distance_km,
                "duration_s": a.duration_s,
                "avg_speed": a.avg_speed,
                "avg_hr": a.avg_hr,
                "max_hr": a.max_hr,
                "aerobic_te": a.aerobic_te,
                "anaerobic_te": a.anaerobic_te,
                "ele_gain_m": a.ele_gain_m,
            }
        )
    return sorted(rows, key=lambda r: r["date"])


def resolve_activities(store, session_state, key="activities"):
    """Load activities once per session, reloading when the store changes.

    Regression guard: the dashboard keeps loaded Activity objects in
    session state across reruns, so a schema migration (or a refresh that
    rewrites rows) otherwise leaves stale objects — e.g. elapsed_s=0.0
    with no lat/lon — in the view pipeline indefinitely, silently emptying
    every metric gated on the new fields. The fingerprint (columns + row
    count + elapsed total) changes on migration, new rows, and value
    rewrites, and forces a reload. session_state is a plain dict here for
    testability; callers pass st.session_state.
    """
    fp = store.activities_fingerprint()
    if session_state.get(key) is None or session_state.get("_activities_fingerprint") != fp:
        session_state[key] = store.load_activities()
        session_state["_activities_fingerprint"] = fp
    return session_state[key]


def run_with_timeout(fn, timeout: float, **kwargs):
    """Run fn on a daemon thread, bounding it to a hard wall-clock timeout.

    Returns fn()'s return value. If fn does not finish within `timeout`
    seconds it raises TimeoutError; the worker thread keeps running in the
    background and its late results are discarded. Exceptions raised by fn
    (other than a timeout) propagate to the caller. Used to keep blocking
    third-party calls (e.g. the Garmin auth/fetch chain) from leaving the
    Streamlit script stuck on a spinner forever when a network call stalls.
    """
    outcome: dict = {}

    def _target() -> None:
        try:
            outcome["value"] = fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised to caller
            outcome["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"operation did not complete within {timeout}s")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def build_session_view(
    activities,
    profile: RunnerProfile,
    lt_payload: dict | None = None,
    race_payload: dict | list | None = None,
    vo2max_payload: list[dict] | None = None,
    series_by_id: dict | None = None,
) -> SessionView:
    rows = compute_metric_rows(
        activities, profile, lt_payload, race_payload, vo2max_payload, series_by_id
    )
    series: dict[str, pd.Series] = {}
    context: dict[str, dict] = {}
    for metric in sorted({r["metric"] for r in rows}):
        mrows = [r for r in rows if r["metric"] == metric]
        context[metric] = {
            "params": _safe_json(mrows[0].get("params")),
            "flags": _safe_json(mrows[0].get("flags")),
            "source": mrows[0].get("source"),
        }
        pairs = [(ts, r["value"]) for r in mrows if (ts := _row_ts(r)) is not None]
        if pairs:
            idx = pd.DatetimeIndex([ts for ts, _ in pairs])
            series[metric] = pd.Series([v for _, v in pairs], index=idx).sort_index()
    return SessionView(profile=profile, series=series, context=context)
