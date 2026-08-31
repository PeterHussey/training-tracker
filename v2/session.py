"""Session-view builder for the dashboard.

Pure (no Streamlit, no network): organizes pipeline.compute_metric_rows into
dated series + interpretation context, and owns the date-filtering / unit-
formatting helpers shared with e2e_report.py so the CLI report and the
dashboard use one implementation.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from pipeline import compute_metric_rows

WEEK_RE = re.compile(r"(\d{4})-W(\d{1,2})")
DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
KM_PER_MI = 1.609344

EXPECTED_METRICS = [
    "volume.distance_total", "load.banister", "load.edwards",
    "pmc.ctl", "pmc.atl", "pmc.tsb", "fitness.vo2max", "load.cs_approx",
]

CONDITIONAL_METRICS = {
    "load.acwr": "requires a >=28d outdoor-running span for the chronic window",
    "load.acwr_pct": "requires a >=28d outdoor-running span for the chronic window",
    "load.banister_cross": "requires cross-training activities with HR data",
    "load.edwards_cross": "requires cross-training activities with HR data",
    "load.lt_hr": "requires a measured lactate threshold in the payload",
    "load.lt_pace": "requires a measured lactate threshold in the payload",
    "race_5k": "requires non-null race predictions in the payload",
    "race_10k": "requires non-null race predictions in the payload",
    "race_half": "requires non-null race predictions in the payload",
    "race_full": "requires non-null race predictions in the payload",
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
    if units == "miles":
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

    profile: object
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


def build_session_view(activities, profile: object, lt_payload: dict | None = None,
                       race_payload: dict | None = None) -> SessionView:
    rows = compute_metric_rows(activities, profile, lt_payload, race_payload)
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
