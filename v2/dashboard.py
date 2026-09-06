"""Training Tracker — interactive dashboard (Streamlit).

Run from v2/:

    ../.venv/bin/streamlit run dashboard.py

Reads a persistent SQLite history DB (activities + runner profile), fetches
fresh data from Garmin on demand, and renders every emitted metric with
interpretation context. All computation lives in session.build_session_view;
this module is a thin view.
"""

import json
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from profile import RunnerProfile, default_profile, with_estimated_hrmax

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gateway import GarminGateway
from normalize import from_summary
from pipeline import persist_session_metrics
from session import _fmt, build_session_view, run_with_timeout
from store import MetricStore

st.set_page_config(page_title="Training Tracker", layout="wide")

DB_PATH = os.environ.get("TRAINING_DB", str(Path(__file__).parent / "data" / "training.sqlite"))
FETCH_DAYS = 365
FETCH_TIMEOUT = 90  # hard cap on the whole Garmin refresh (auth + fetches)
DEFAULT_WINDOW_DAYS = 180
PERIOD_KEY = "period"
CACHE_DIR_NAME = "app_cache"
APP_CACHE_DIR = Path("cache") / CACHE_DIR_NAME

KPI_KEYS = [
    ("load.acwr", "ACWR"),
    ("load.acwr_pct", "ACWR %"),
    ("pmc.ctl", "CTL"),
    ("pmc.atl", "ATL"),
    ("pmc.tsb", "TSB"),
    ("volume.distance_total", "Weekly"),
    ("fitness.vo2max", "VO2max"),
    ("load.lt_hr", "LT HR"),
    ("load.banister", "Daily load"),
]


GLOSSARY = [
    "Prefer TREND over absolute value for every metric.",
    "Banister TRIMP = minutes * dHR * 0.64 * exp(b*dHR); b = 1.92 (M) / 1.67 (F). "
    "Raising the chosen HRmax lowers every TRIMP value.",
    "Daily load stacks Banister TRIMP across outdoor running, treadmill, and "
    "cross-training (HR intensity only). The combined total feeds PMC/ACWR.",
    "CTL (tau 42d) = fitness; ATL (tau 7d) = fatigue; TSB = CTL - ATL, positive = form.",
    "ACWR = mean daily load (7d) / mean daily load (28d), coupled. It measures load "
    "SWING, not injury prediction; the green 0.8-1.3 band is a heuristic. ACWR now "
    "uses the combined running+treadmill+cross load.",
    "VO2max is a Firstbeat estimate (~5% error, underestimates >=60 mL/kg/min); never "
    "recomputed here.",
    "LT heart rate anchors (~7% error); LT pace can overestimate 20-26%.",
    "Race predictions: 5K/10K/half are the trustworthy end; marathon least.",
    "Cross-training and strength have no distance, so they are excluded from distance "
    "volume but are reported separately in the time-volume (hours/week) chart. "
    "Cross-training is included in HR load; strength is duration-only.",
    "Elevation is route context, not a risk metric.",
    "Garmin proprietary load/TE and ACWR bands are reference, never gates.",
    "Aerobic decoupling needs >=6 route-matched flat sessions; single-run values are "
    "noise and are not shown here.",
]


def compute_fetch_start(
    end: date, latest_activity_date: date | None, fetch_days: int = FETCH_DAYS
) -> date:
    """Start date for an incremental Garmin refresh.

    Subsequent refreshes fetch only activities newer than the latest date already
    in the local store (bounded to the last `fetch_days` window for safety): the
    first run, when the store is empty (`latest_activity_date is None`), fetches
    the full `fetch_days` window to populate history. This bounds the page count
    of 100-row paginations so a large history no longer blows the 90s guard.
    """
    if latest_activity_date is None:
        return end - timedelta(days=fetch_days)
    return max(end - timedelta(days=fetch_days), latest_activity_date + timedelta(days=1))


def period_bounds(
    dates: list[date], window_days: int = DEFAULT_WINDOW_DAYS
) -> tuple[date, date, date]:
    """(min_d, since, end) for the Period selector.

    min_d/end are the store's earliest/latest activity dates; since is the
    trailing-window default, floored to min_d. Independent of today's date.
    """
    min_d = min(dates)
    end = max(dates)
    since = max(min_d, end - timedelta(days=window_days))
    return min_d, since, end


@st.cache_resource
def get_store() -> MetricStore:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return MetricStore(DB_PATH)


def _load_json_file(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _latest_json_file(cache: Path, pattern: str):
    try:
        candidates = [p for p in cache.glob(pattern) if p.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return _load_json_file(candidates[0])


def _persist_selected_fields(store: MetricStore, selected_race: str) -> None:
    """Persist user-selected race distance to the runner profile."""
    profile = store.load_runner_profile()
    if profile is None:
        profile = default_profile()
    if profile.selected_race != selected_race:
        store.save_runner_profile(replace(profile, selected_race=selected_race))


def load_cached_trends(
    cache_dir: Path = APP_CACHE_DIR,
) -> tuple[dict | None, dict | list | None, list | None]:
    """Rehydrate the last-fetched Garmin trend payloads from the gateway cache.

    The gateway persists each fetch to cache/app_cache/ (race_predictions.json,
    lactate_threshold.json, vo2max_trend_*.json), but the dashboard previously
    kept fresh payloads only in st.session_state — so every app restart lost
    the race predictions until the next refresh. Returns (lt, race, vo2max)
    with None for anything not cached.
    """
    cache = Path(cache_dir)
    if not cache.is_dir():
        return None, None, None
    lt = _load_json_file(cache / "lactate_threshold.json")
    # race payload: prefer the daily-trend file (a list) — that's what the
    # chart now plots. Fall back to the legacy latest-snapshot dict if no
    # trend file exists yet (a dict is still valid for the pipeline).
    race: dict | list | None = _latest_json_file(cache, "race_predictions_trend_*.json")
    if race is None:
        latest = _load_json_file(cache / "race_predictions.json")
        race = latest if isinstance(latest, (dict, list)) else None
    vo2: list | None = None
    try:
        candidates = [p for p in cache.glob("vo2max_trend_*.json") if p.is_file()]
    except OSError:
        candidates = []
    if candidates:
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        latest = _load_json_file(candidates[0])
        vo2 = latest if isinstance(latest, list) else None
    return lt, race, vo2


def refresh_garmin(fetch_mode: str = "incremental") -> tuple[list, dict, list, list]:
    """Fetch activities from Garmin.

    Returns (activities, lt_payload, race_trend, vo2max_trend). race_trend is
    the full-window daily list from /racepredictions/daily (not the single
    /latest snapshot), so the predictions chart plots a real time series.

    Args:
        fetch_mode: "incremental" to fetch only activities newer than the latest
            persisted date; "historical" to fetch older activities up to the API's
            100-activity per-page limit.
    """
    end = date.today()
    store = get_store()
    if fetch_mode == "historical":
        earliest = store.earliest_activity_date()
        if earliest is None:
            start = end - timedelta(days=FETCH_DAYS)
        else:
            start = max(date.min, earliest - timedelta(days=FETCH_DAYS))
        fetch_end = earliest
    else:
        start = compute_fetch_start(end, store.latest_activity_date())
        fetch_end = end
    gw = GarminGateway(cache_dir=APP_CACHE_DIR)
    with st.spinner("Fetching activities from Garmin..."):
        raw = gw.fetch_activities(start.isoformat(), fetch_end.isoformat())
    lt = gw.fetch_lactate_threshold() or {}
    # Trend payloads are single (non-paginated) daily-summary calls, so they
    # always cover the full window — NOT the incremental activities window.
    # Binding them to `start` would yield only the days since the last refresh
    # (e.g. 1-2 points on most refreshes), producing uselessly short charts.
    # Race uses the daily history endpoint (not /latest, which is a single
    # current snapshot) so the predictions chart has a real trend.
    trend_start = end - timedelta(days=FETCH_DAYS)
    race = gw.fetch_race_predictions_trend(trend_start.isoformat(), end.isoformat()) or []
    vo2 = gw.fetch_vo2max_trend(trend_start.isoformat(), end.isoformat()) or []
    # An empty incremental window is normal: compute_fetch_start only asks for
    # activities newer than the latest persisted one, so "nothing new since the
    # last refresh" legitimately returns []. Only a truly empty account (no local
    # data AND nothing from Garmin) is a fetch failure.
    if not raw:
        if store.latest_activity_date() is None:
            raise RuntimeError("Garmin returned no activities")
        if fetch_mode == "historical":
            raise RuntimeError("No older activities found in this date range")
        # Incremental mode with no new activities is a normal no-op.
        return [], lt, race, vo2
    acts = [from_summary(a) for a in raw]
    return acts, lt, race, vo2


DETAILS_TOP_K = 40
DETAILS_TIMEOUT_S = 8
DETAILS_TOTAL_TIMEOUT_S = 60


def fetch_details_for_lthr(
    gw: GarminGateway,
    activities: list,
    top_k: int = DETAILS_TOP_K,
    timeout_s: float = DETAILS_TIMEOUT_S,
) -> dict[int, tuple]:
    """Fetch activity details for the top-k outdoor running activities.

    Checks disk cache first. For cache misses, fetches with per-request timeout.
    Never blocks the refresh — skips on failure.

    Returns:
        {activity_id: (hr_list, speed_list)} for successfully fetched activities.
    """
    from metrics.threshold import parse_details_series

    candidates = [
        a
        for a in activities
        if a.sport == "running" and a.duration_s >= 1200 and a.avg_hr is not None
    ]
    candidates.sort(key=lambda a: a.avg_speed or 0, reverse=True)
    candidates = candidates[:top_k]

    series_by_id: dict[int, tuple] = {}
    for a in candidates:
        cache_path = gw.cache_dir / f"activity_details_{a.activity_id}.json"
        if cache_path.exists():
            try:
                details = json.loads(cache_path.read_text())
                hr, spd = parse_details_series(details)
                series_by_id[a.activity_id] = (hr, spd)
            except (json.JSONDecodeError, OSError):
                pass
            continue
        try:
            details = run_with_timeout(
                gw.fetch_activity_details, timeout=timeout_s, activity_id=a.activity_id
            )
            hr, spd = parse_details_series(details)
            series_by_id[a.activity_id] = (hr, spd)
        except (TimeoutError, OSError, json.JSONDecodeError, KeyError):
            pass
    return series_by_id


def profile_from_widgets(activities, persisted: RunnerProfile | None = None) -> RunnerProfile:
    p = persisted or default_profile()
    sex = st.sidebar.selectbox("Sex", ("M", "F"), index=0 if p.sex == "M" else 1)
    birth_year = st.sidebar.number_input(
        "Birth year", min_value=1920, max_value=2100, value=p.birth_year
    )
    hrrest = st.sidebar.number_input(
        "Resting HR (bpm)", min_value=30, max_value=120, value=p.hrrest
    )
    HRMAX_SOURCE_INDEX = {"configured": 0, "observed": 1, "age_predicted": 2}
    src = st.sidebar.radio(
        "HRmax source",
        ("manual", "estimate from workouts", "age-predicted"),
        index=HRMAX_SOURCE_INDEX.get(p.hrmax_source, 2),
    )
    selected_race = st.sidebar.selectbox(
        "Race distance",
        ("5k", "10k", "half", "full"),
        index=("5k", "10k", "half", "full").index(p.selected_race) if p.selected_race else 0,
    )
    hrmax_val, hrmax_src_label = get_hrmax_display(src, birth_year, hrrest, sex, p, activities)
    st.sidebar.caption(f"HRmax: {hrmax_val} · Source: {hrmax_src_label}")
    if src == "manual":
        default_hrmax = (
            p.hrmax
            if p.hrmax_source == "configured"
            else 220 - (date.today().year - int(birth_year))
        )
        hrmax = st.sidebar.number_input(
            "HRmax (bpm)", min_value=110, max_value=240, value=default_hrmax
        )
        return replace(
            RunnerProfile(
                hrmax=int(hrmax),
                hrrest=int(hrrest),
                sex=sex,
                birth_year=int(birth_year),
                hrmax_source="configured",
            ),
            selected_race=selected_race,
        )
    base = RunnerProfile.from_age(
        age=date.today().year - int(birth_year),
        hrrest=int(hrrest),
        sex=sex,
        birth_year=int(birth_year),
    )
    if src == "estimate from workouts":
        return replace(with_estimated_hrmax(base, activities), selected_race=selected_race)
    return replace(base, selected_race=selected_race)


def profile_sig(p: RunnerProfile) -> tuple:
    return (p.hrmax, p.hrrest, p.sex, p.birth_year, p.hrmax_source, p.units)


def activities_sig(acts) -> tuple:
    return tuple(
        (
            a.activity_id,
            a.sport,
            a.date.isoformat(),
            a.distance_m,
            a.duration_s,
        )
        for a in acts
    )


def get_hrmax_display(
    src: str,
    birth_year: int,
    hrrest: int,
    sex: str,
    profile: RunnerProfile,
    activities,
) -> tuple[int, str]:
    """Return (hrmax_value, source_label) for the current radio selection."""
    if src == "manual":
        default = (
            profile.hrmax
            if profile.hrmax_source == "configured"
            else 220 - (date.today().year - int(birth_year))
        )
        return default, "configured"
    base = RunnerProfile.from_age(
        age=date.today().year - int(birth_year),
        hrrest=int(hrrest),
        sex=sex,
        birth_year=int(birth_year),
    )
    if src == "estimate from workouts":
        est = with_estimated_hrmax(base, activities)
        if est.hrmax_source == "observed":
            return est.hrmax, "estimated from workouts"
        return est.hrmax, "age-predicted (no observed HR)"
    return base.hrmax, "age-predicted"


def context_line(view, metric) -> str:
    meta = view.context.get(metric, {})
    parts = [f"source: {meta.get('source', '?')}"]
    for key in ("params", "flags"):
        for k, v in sorted((meta.get(key) or {}).items()):
            parts.append(f"{k}={v}")
    return "  |  ".join(parts)


def last_value(s: pd.Series):
    s = s.dropna()
    return float(s.iloc[-1]) if len(s) else None


def pace_per_unit(s: pd.Series, units: str) -> pd.Series:
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    return m_per_unit / s


def pace_min_per_unit(s: pd.Series, units: str) -> pd.Series:
    """Convert m/s pace to min per km or min per mile (decimal minutes)."""
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    return (m_per_unit / s) / 60.0


def _fmt_pace_min(v: float, units: str) -> str:
    """Format decimal minutes as MM:SS /unit."""
    total_sec = v * 60
    m, s = divmod(int(total_sec), 60)
    unit = "/mi" if units in ("miles", "imperial") else "/km"
    return f"{m}:{s:02d} {unit}"


def _anchor_cards(windowed, view, units) -> None:
    """Render best-effort LTHR anchor cards with qualifier dot scatters."""
    anchors = [
        (
            "load.lt_hr_best20",
            "load.lt_pace_best20",
            "load.lt_effort_dots20_hr",
            "load.lt_effort_dots20_pace",
            "20-min",
        ),
        (
            "load.lt_hr_best30",
            "load.lt_pace_best30",
            "load.lt_effort_dots30_hr",
            "load.lt_effort_dots30_pace",
            "30-min",
        ),
    ]
    has_any = False
    for hr_key, pace_key, dots_hr_key, dots_pace_key, label in anchors:
        hr_s = windowed.get(hr_key)
        if hr_s is None or hr_s.empty:
            continue
        has_any = True
        proxy_hr = float(hr_s.iloc[-1])
        meta = view.context.get(hr_key, {})
        params = meta.get("params", {})
        factor = params.get("factor")
        raw_hr = proxy_hr / factor if factor and factor != 0 else proxy_hr
        pace_s = windowed.get(pace_key)
        pace_val = float(pace_s.iloc[-1]) if pace_s is not None and not pace_s.empty else None
        pace_str = _fmt_pace_min(pace_val * 60, units) if pace_val else "—"
        dt = hr_s.index[-1]
        date_str = dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt)

        card_text = f"{proxy_hr:.0f} bpm ({raw_hr:.0f} raw @ {pace_str}, {date_str})"
        st.metric(f"LTHR anchor {label}", card_text)

        # Qualifier dots: faint scatter of all qualifying efforts
        dots_hr = windowed.get(dots_hr_key)
        dots_pace = windowed.get(dots_pace_key)
        if (
            dots_hr is not None
            and not dots_hr.empty
            and dots_pace is not None
            and not dots_pace.empty
        ):
            hr_vals = dots_hr.values
            hr_labels = [f"{v:.0f} bpm" for v in hr_vals]
            pace_vals = pace_min_per_unit(dots_pace, units)
            pace_labels = [_fmt_pace_min(v, units) for v in pace_vals]
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=dots_hr.index,
                    y=hr_vals,
                    mode="markers",
                    name=f"{label} HR",
                    text=hr_labels,
                    hovertemplate="%{x}<br>%{text}",
                    opacity=0.35,
                    yaxis="y",
                )
            )
            fig.add_trace(
                go.Scatter(
                    x=dots_pace.index,
                    y=pace_vals,
                    mode="markers",
                    name=f"{label} pace",
                    text=pace_labels,
                    hovertemplate="%{x}<br>%{text}",
                    opacity=0.35,
                    yaxis="y2",
                )
            )
            fig.update_layout(
                title=f"Qualifier dots — {label} window",
                yaxis_title="bpm",
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"min per {units}",
                },
                hovermode="x unified",
                showlegend=False,
            )
            apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
            st.plotly_chart(fig, use_container_width=True)
            st.caption(context_line(view, dots_hr_key))

    if not has_any:
        st.write(
            "LT heart rate / pace require a measured lactate threshold in "
            "the Garmin payload (appears after a Refresh)."
        )


def race_time_ticks(values, count: int = 6) -> tuple[list[float], list[str]]:
    """Evenly spaced H:MM:SS y-axis ticks covering `values` (seconds).

    Plotly has no duration tick formatter, so the race chart passes raw
    seconds as y values and labels the axis explicitly via tickvals/ticktext.
    Steps snap to whole minutes/hours so labels stay round; the first tick is
    at or below the minimum value and the last at or above the maximum.
    """
    import math

    vals = [float(v) for v in values if v is not None and v == v]
    if not vals:
        return [], []
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        lo = max(0.0, hi - 60.0)
    raw_step = (hi - lo) / max(count - 1, 1)
    step = 7200.0
    for nice in (60.0, 300.0, 600.0, 900.0, 1800.0, 3600.0, 7200.0):
        if nice >= raw_step:
            step = nice
            break
    first = math.floor(lo / step) * step
    ticks = [first + i * step for i in range(int(math.ceil((hi - first) / step)) + 1)]
    return ticks, [_fmt(t, {"unit": "s"}, "km") for t in ticks]


def apply_yaxis_mode(fig: go.Figure, mode: str) -> go.Figure:
    """Apply the user's y-axis scale preference to a Plotly figure.

    mode: "auto" (Plotly picks the range from the data, the default) or
    "fit to data" (same as auto; future option for fixed/manual ranges).
    """
    if mode == "fit to data":
        # Plotly auto-fits by default; explicit range=None lets the data drive it.
        for axis in ("yaxis", "yaxis2"):
            if axis in fig.layout:
                fig.layout[axis].range = None
    return fig


def render_kpis(windowed, view, units, selected_kpi_labels: list[str] | None = None) -> None:
    visible = [
        (k, lbl) for k, lbl in KPI_KEYS if selected_kpi_labels is None or lbl in selected_kpi_labels
    ]
    cols = st.columns(len(visible))
    for col, (key, label) in zip(cols, visible, strict=False):
        s = windowed.get(key)
        val = last_value(s) if s is not None else None
        if val is None:
            col.metric(label, "—")
            continue
        if key == "volume.distance_total":
            label = f"Weekly {'mi' if units in ('miles', 'imperial') else 'km'}"
        meta = (view.context.get(key) or {}).get("params", {})
        col.metric(label, _fmt(val, meta, units))


def render_load_tab(view, windowed, units) -> None:
    sport_series = [
        ("load.banister_running", "Outdoor running"),
        ("load.banister_treadmill", "Treadmill"),
        ("load.banister_cross", "Cross-training"),
    ]
    plotted = [
        (k, n, windowed.get(k))
        for k, n in sport_series
        if windowed.get(k) is not None and not windowed.get(k).empty
    ]
    if plotted:
        show_ed = st.checkbox("Overlay combined Edwards TRIMP")
        fig = go.Figure()
        for _key, name, s in plotted:
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=name))
        if show_ed:
            e = windowed.get("load.edwards")
            if e is not None and len(e):
                fig.add_trace(go.Scatter(x=e.index, y=e.values, name="Edwards (total)", yaxis="y2"))
                fig.update_layout(
                    yaxis2={"overlaying": "y", "side": "right", "title": "Edwards TRIMP"}
                )
        fig.update_layout(
            barmode="stack",
            title="Daily Banister TRIMP (stacked by sport)",
            hovermode="x unified",
            yaxis_title="Banister TRIMP",
            legend={"orientation": "h", "y": 1.12},
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Stacked across running + treadmill + cross-training (HR intensity only). "
            "The sum feeds the PMC/ACWR windows below. " + context_line(view, "load.banister")
        )
    else:
        s = windowed.get("load.banister")
        if s is None or s.empty:
            st.write("No HR-load activities (running/treadmill/cross with HR data) in this window.")
        else:
            st.write("Per-sport daily load is empty; combined total exists but shows no detail.")

    tsb = windowed.get("pmc.tsb")
    if tsb is None or tsb.empty:
        st.write("PMC chart needs combined daily load history in this window.")
    else:
        fig = go.Figure()
        for key, name, color in (("pmc.ctl", "CTL", "#2E86AB"), ("pmc.atl", "ATL", "#A23B72")):
            t = windowed.get(key)
            if t is not None and len(t):
                fig.add_trace(go.Scatter(x=t.index, y=t.values, name=name, line={"color": color}))
        fig.add_trace(
            go.Scatter(x=tsb.index, y=tsb.values, name="TSB", yaxis="y2", line={"color": "#F18F01"})
        )
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        fig.update_layout(
            title="Fitness (CTL) / Fatigue (ATL) / Form (TSB)",
            hovermode="x unified",
            yaxis_title="Combined TRIMP / day",
            yaxis2={"overlaying": "y", "side": "right", "title": "TSB"},
            legend={"orientation": "h", "y": 1.12},
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "TSB = CTL - ATL; positive = fresh/form. Combined load windows. "
            "tau 42/7 days. " + context_line(view, "pmc.ctl")
        )

    acwr = windowed.get("load.acwr")
    if acwr is None or acwr.empty:
        st.write("ACWR needs a >=28-day combined-load span to build the chronic window.")
    else:
        fig = go.Figure()
        fig.add_hrect(y0=0.8, y1=1.3, fillcolor="lightgreen", opacity=0.2, line_width=0)
        fig.add_trace(
            go.Scatter(x=acwr.index, y=acwr.values, name="ACWR", line={"color": "#2E86AB"})
        )
        pct = windowed.get("load.acwr_pct")
        if pct is not None and len(pct):
            fig.add_trace(
                go.Scatter(
                    x=pct.index,
                    y=pct.values * 100,
                    name="history pct (%)",
                    yaxis="y2",
                    line={"color": "#F18F01", "dash": "dash"},
                )
            )
            fig.update_layout(
                yaxis2={"overlaying": "y", "side": "right", "title": "% within last 180d"}
            )
        fig.add_hline(y=0.5, line_dash="dot", line_color="red")
        fig.add_hline(y=1.5, line_dash="dot", line_color="red")
        fig.update_layout(
            title="Acute:Chronic Workload Ratio (coupled 7/28)",
            hovermode="x unified",
            yaxis={"range": [0, max(2.0, float(acwr.max()))]},
            legend={"orientation": "h", "y": 1.12},
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Green 0.8-1.3 is a heuristic. ACWR measures load SWING, not "
            "injury prediction. Computed from combined running+treadmill+cross "
            "load. " + context_line(view, "load.acwr")
        )


def render_fitness_tab(view, windowed, units, selected_race: str = "5k") -> None:
    vo2 = windowed.get("fitness.vo2max")
    if vo2 is None or vo2.empty:
        st.write(
            "No VO2max trend data. Use **Refresh from Garmin** to pull the "
            "daily trend from `/maxmet/daily` (Garmin running VO2max)."
        )
    else:
        fig = go.Figure(go.Scatter(x=vo2.index, y=vo2.values, mode="lines+markers", name="VO2max"))
        fig.update_layout(title="VO2max (Firstbeat estimate, daily trend)", hovermode="x unified")
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Daily trend from Garmin `/maxmet/daily` (vo2MaxPreciseValue). "
            "Firstbeat estimate ~5% error, underestimates >=60 mL/kg/min. "
            + context_line(view, "fitness.vo2max")
        )

    threshes = [
        ("load.lt_hr", "LT heart rate (Garmin)", {"unit": "bpm"}),
        ("load.lt_pace", "LT pace (Garmin)", {"unit": "m/s"}),
        ("load.cs_approx", "Approx critical velocity (fastest mile)", {"unit": "m/s"}),
    ]
    for key, name, params in threshes:
        s = windowed.get(key)
        if s is None or s.empty:
            continue
        if params and params.get("unit") == "m/s":
            y = pace_min_per_unit(s, units)
            labels = [_fmt_pace_min(v, units) for v in y]
        else:
            y = s.values
            labels = [_fmt(v, params or {"unit": "s"}, units) for v in y]
        fig = go.Figure(
            go.Scatter(
                x=s.index,
                y=y,
                mode="lines+markers",
                name=name,
                text=labels,
                hovertemplate="%{x}<br>%{text}",
            )
        )
        fig.update_layout(
            title=name,
            yaxis_title=("bpm" if params and params.get("unit") != "m/s" else f"min per {units}"),
            hovermode="x unified",
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(context_line(view, key))

    # Best-effort LTHR anchor cards + qualifier dots
    _anchor_cards(windowed, view, units)

    preds = {d: windowed.get(f"race_{d}") for d in ("5k", "10k", "half", "full")}
    if any(p is not None and len(p) for p in preds.values()):
        race_dist = st.segmented_control(
            "Race distance",
            ("5k", "10k", "half", "full"),
            default=selected_race,
            format_func=lambda d: {"5k": "5K", "10k": "10K", "half": "Half", "full": "Full"}[d],
            key="race_distance_selector",
        )
        if race_dist is not None and race_dist != selected_race:
            profile = get_store().load_runner_profile()
            if profile is not None and profile.selected_race != race_dist:
                get_store().save_runner_profile(replace(profile, selected_race=race_dist))
        focus = race_dist or selected_race
        s = preds.get(focus)
        if s is None or len(s) == 0:
            s = next(
                (
                    ps
                    for d in ("5k", "10k", "half", "full")
                    if (ps := preds.get(d)) is not None and len(ps)
                ),
                None,
            )
        if s is None or len(s) == 0:
            st.write(
                "Race predictions appear after a Refresh (requires new Garmin "
                "predictions in the payload)."
            )
            return
        labels = [_fmt(v, {"unit": "s"}, units) for v in s.values]
        fig = go.Figure(
            go.Scatter(
                x=s.index,
                y=s.values,
                mode="lines+markers",
                line_shape="hv",
                name=f"{focus} race",
                text=labels,
                hovertemplate="%{x}<br>%{text}",
            )
        )
        tickvals, ticktext = race_time_ticks([float(v) for v in s.values])
        fig.update_layout(
            title=f"Garmin race predictions — {focus}",
            hovermode="x unified",
            yaxis_title="predicted time",
            yaxis={"tickvals": tickvals, "ticktext": ticktext},
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Daily history from `/racepredictions/daily` — each distance "
            "trends over the year, so a single refresh shows the prediction "
            "time series, not just today's snapshot. "
            "5K/10K/half are the trustworthy end; marathon is the least trustworthy prediction."
        )
    else:
        st.write(
            "Race predictions appear after a Refresh (requires new Garmin "
            "predictions in the payload)."
        )


def render_volume_tab(activities, view, windowed, units) -> None:
    weeks = {g: windowed.get(f"volume.distance_{g}") for g in ("running", "treadmill", "cross")}
    weeks = {g: s for g, s in weeks.items() if s is not None and len(s)}
    if weeks:
        fig = go.Figure()
        for g, s in weeks.items():
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=g))
        roll = windowed.get("volume.rolling4wk_total")
        if roll is not None and len(roll):
            fig.add_trace(
                go.Scatter(
                    x=roll.index,
                    y=roll.values,
                    name="rolling 4wk",
                    yaxis="y2",
                    line={"color": "#F18F01"},
                )
            )
            fig.update_layout(
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"rolling {'mi' if units in ('miles', 'imperial') else 'km'}",
                }
            )
        fig.update_layout(title="Weekly distance", barmode="stack", hovermode="x unified")
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Stacked by sport. Cross-training has no distance and stacks "
            "at 0; see the time-volume chart for its workload. "
            + context_line(view, "volume.distance_total")
        )
    else:
        st.write("No volume data in this window.")

    # Time-volume chart (hours/week) — includes cross-training, which has no
    # distance but still consumes time/energy.
    hours = {
        g: windowed.get(f"volume.duration_{g}")
        for g in ("running", "treadmill", "cross", "strength")
    }
    hours = {g: s for g, s in hours.items() if s is not None and len(s)}
    if hours:
        fig = go.Figure()
        for g, s in hours.items():
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=g))
        roll = windowed.get("volume.rolling4wk_duration_total")
        if roll is not None and len(roll):
            fig.add_trace(
                go.Scatter(
                    x=roll.index,
                    y=roll.values,
                    name="rolling 4wk",
                    yaxis="y2",
                    line={"color": "#F18F01"},
                )
            )
            fig.update_layout(yaxis2={"overlaying": "y", "side": "right", "title": "rolling hours"})
        fig.update_layout(
            title="Weekly duration (hours)",
            barmode="stack",
            hovermode="x unified",
            yaxis_title="hours",
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "Time-anchored workload: running + treadmill + cross-training + strength. "
            "Cross-training and strength contribute time even with no distance. "
            + context_line(view, "volume.duration_total")
        )

        wow = windowed.get("volume.wow_pct_duration_total")
        if wow is not None and len(wow):
            fig = go.Figure(go.Bar(x=wow.index, y=wow.values * 100, name="week-over-week %"))
            fig.add_hline(y=0, line_dash="dot", line_color="gray")
            fig.update_layout(
                title="Week-over-week duration change", hovermode="x unified", yaxis_title="%"
            )
            apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)

    wow = windowed.get("volume.wow_pct_total")
    if wow is not None and len(wow):
        fig = go.Figure(go.Bar(x=wow.index, y=wow.values * 100, name="week-over-week %"))
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        fig.update_layout(
            title="Week-over-week distance change", hovermode="x unified", yaxis_title="%"
        )
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)

    gain = windowed.get("elevation.daily_gain_running")
    if gain is not None and len(gain):
        fig = go.Figure(
            go.Bar(
                x=gain.index,
                y=gain.values,
                name=f"daily gain ({'ft' if units in ('miles', 'imperial') else 'm'})",
            )
        )
        roll28 = windowed.get("elevation.rolling28d_running")
        if roll28 is not None and len(roll28):
            fig.add_trace(
                go.Scatter(
                    x=roll28.index, y=roll28.values, name="rolling 28d", line={"color": "#F18F01"}
                )
            )
        per_km = windowed.get("elevation.gain_per_km_running")
        if per_km is not None and len(per_km):
            fig.add_trace(
                go.Scatter(
                    x=per_km.index,
                    y=per_km.values,
                    name=f"{'ft' if units in ('miles', 'imperial') else 'm'} / {'mi' if units in ('miles', 'imperial') else 'km'}",
                    yaxis="y2",
                    line={"color": "#2E86AB", "dash": "dash"},
                )
            )
            fig.update_layout(
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"{'ft' if units in ('miles', 'imperial') else 'm'} / {'mi' if units in ('miles', 'imperial') else 'km'}",
                }
            )
        fig.update_layout(title="Elevation gain (running)", hovermode="x unified")
        apply_yaxis_mode(fig, st.session_state.get("_yaxis_mode", "auto"))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Elevation is route context, not a risk metric.")
    else:
        st.write("No elevation data in this window.")


def render_repetitions(activities, activity_id: int, units: str) -> None:
    """Comparison table + chart for every repetition of the selected workout.

    The selected activity's 80/20 plan code (e.g. 'RF24') is the grouping key;
    the table and line chart show all repetitions with that same code, oldest
    first. Activities without a code show a hint instead.
    """
    target = next((a for a in activities if a.activity_id == activity_id), None)
    if target is None or target.code is None:
        st.caption(
            "This activity has no 80/20 plan workout code in its name "
            "(e.g. 'Winnetka - RF24 (Foundation Run)'), so there is nothing to compare."
        )
        return
    code = target.code
    reps = [a for a in activities if a.code == code]
    reps.sort(key=lambda a: a.date)
    if len(reps) < 2:
        st.caption(f"Only one repetition of **{code}** found — add more to compare.")
        return
    table = []
    for a in reps:
        m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
        pace = m_per_unit / a.avg_speed if a.avg_speed else None
        table.append(
            {
                "date": a.date.isoformat(),
                "distance": _fmt(a.distance_km, {"unit": "km"}, units),
                "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
                "avg HR": f"{a.avg_hr:.0f}" if a.avg_hr is not None else "—",
                "max HR": f"{a.max_hr:.0f}" if a.max_hr is not None else "—",
                "aerob. TE": f"{a.aerobic_te:.1f}" if a.aerobic_te is not None else "—",
                "anaerob. TE": f"{a.anaerobic_te:.1f}" if a.anaerobic_te is not None else "—",
                "elev gain": _fmt(a.ele_gain_m, {"unit": "m"}, units)
                if a.ele_gain_m is not None
                else "—",
            }
        )
    st.subheader(f"Repeated workout **{code}**")
    st.dataframe(table, use_container_width=True, hide_index=True)

    xs = [a.date for a in reps]
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    paces = [m_per_unit / a.avg_speed / 60.0 if a.avg_speed else None for a in reps]
    hrs = [a.avg_hr for a in reps]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=paces, mode="lines+markers", name="avg pace (min/unit)"))
    fig.add_trace(
        go.Scatter(x=xs, y=hrs, mode="lines+markers", name="avg HR (bpm)", yaxis="y2"),
    )
    fig.update_layout(
        title=f"Performance over repetitions — {code}",
        hovermode="x unified",
        yaxis_title=f"min / {'mi' if units not in ('km', 'metric') else 'km'}",
        yaxis2={"overlaying": "y", "side": "right", "title": "bpm"},
        legend={"orientation": "h", "y": 1.12},
    )
    st.plotly_chart(fig, use_container_width=True)


def render_activities(activities, since, until, units) -> None:
    rows = []
    for a in activities:
        if not (since <= a.date <= until):
            continue
        pace = None
        if a.avg_speed:
            m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
            pace = m_per_unit / a.avg_speed
        rows.append(
            {
                "date": a.date.isoformat(),
                "sport": a.sport,
                "name": a.name or "—",
                "distance": _fmt(a.distance_km, {"unit": "km"}, units),
                "duration": _fmt(a.duration_s, {"unit": "s"}, units),
                "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
                "avg HR": f"{a.avg_hr:.0f}" if a.avg_hr is not None else "—",
                "max HR": f"{a.max_hr:.0f}" if a.max_hr is not None else "—",
                "VO2max": f"{a.vo2max:.1f}" if a.vo2max is not None else "—",
                "elev gain": _fmt(a.ele_gain_m, {"unit": "m"}, units)
                if a.ele_gain_m is not None
                else "—",
                "activity_id": a.activity_id,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        st.write("No activities in this window.")
        return
    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    st.caption("Select a row to compare repeated workouts (same 80/20 plan code).")
    sel = st.dataframe(
        df.drop(columns=["activity_id"]),
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="activities_table",
    )
    if sel.selection.rows:
        selected_id = int(df.iloc[sel.selection.rows[0]]["activity_id"])
        render_repetitions(activities, selected_id, units)


def main() -> None:
    st.title("Training Tracker")
    store = get_store()

    persisted_profile = store.load_runner_profile()
    _units_display = "km" if (persisted_profile or default_profile()).units == "metric" else "miles"
    units = st.sidebar.radio("Units", ("km", "miles"), index=0 if _units_display == "km" else 1)

    if "activities" not in st.session_state:
        st.session_state["activities"] = store.load_activities()
        st.session_state["lt_payload"] = None
        st.session_state["race_payload"] = None
        st.session_state["vo2max_payload"] = None
        # First run in this session: rehydrate the last-fetched trend payloads
        # from the gateway cache so race predictions etc. survive restarts.
        cached_lt, cached_race, cached_vo2 = load_cached_trends()
        if cached_lt is not None:
            st.session_state["lt_payload"] = cached_lt
        if cached_race is not None:
            st.session_state["race_payload"] = cached_race
        if cached_vo2 is not None:
            st.session_state["vo2max_payload"] = cached_vo2

    st.sidebar.header("Data")
    fetch_mode = st.sidebar.radio(
        "Fetch mode",
        ("incremental", "historical"),
        index=0,
        format_func=lambda x: (
            "Incremental (new only)" if x == "incremental" else "Historical (up to 100)"
        ),
    )

    if st.sidebar.button("Refresh from Garmin"):
        try:
            acts, lt, race, vo2 = run_with_timeout(
                refresh_garmin, timeout=FETCH_TIMEOUT, fetch_mode=fetch_mode
            )
            if acts:
                store.save_activities(acts)
                st.session_state["activities"] = store.load_activities()
                st.session_state.pop(PERIOD_KEY, None)
            # Trend payloads update even when there are no new activities.
            st.session_state["lt_payload"] = lt
            st.session_state["race_payload"] = race
            st.session_state["vo2max_payload"] = vo2
            # Fan out to fetch activity details for best-effort LTHR anchors.
            gw = GarminGateway(cache_dir=APP_CACHE_DIR)
            activities_for_details = st.session_state["activities"]
            st.session_state["series_by_id"] = run_with_timeout(
                fetch_details_for_lthr,
                timeout=DETAILS_TOTAL_TIMEOUT_S,
                gw=gw,
                activities=activities_for_details,
            )
            if fetch_mode == "historical":
                msg = f"Historical fetch: {len(acts)} activities fetched"
            else:
                msg = (
                    f"Fetched {len(acts)} activities"
                    if acts
                    else "No new activities; Garmin trend data refreshed"
                )
            st.sidebar.success(msg)
        except Exception as exc:
            st.sidebar.warning(f"Garmin fetch failed: {exc.__class__.__name__}: {exc}")

    activities = st.session_state["activities"]
    if not activities:
        st.info(
            "No data yet. Click **Refresh from Garmin** in the sidebar to "
            "pull your activity history into the local database, then set "
            "your profile inputs. Everything stays local except that initial "
            "Garmin pull."
        )
        st.stop()

    st.sidebar.header("Profile")
    units_stored = "metric" if units == "km" else "imperial"
    profile = replace(profile_from_widgets(activities, persisted_profile), units=units_stored)
    psig = profile_sig(profile)
    if st.session_state.get("_profile_sig") != psig:
        store.save_runner_profile(profile)
        st.session_state["_profile_sig"] = psig

    st.sidebar.header("Chart axes")
    yaxis_mode = st.sidebar.radio(
        "Y-axis scale",
        ("auto", "fit to data"),
        index=0,
        help=(
            "auto: Plotly picks the range from the data (default). "
            "fit to data: same as auto; future option for fixed/manual ranges."
        ),
        key="yaxis_mode",
    )
    st.session_state["_yaxis_mode"] = yaxis_mode

    st.sidebar.header("Selected KPI fields")
    all_kpi_labels = [label for _key, label in KPI_KEYS]
    default_kpi_labels = st.session_state.get("selected_kpi_labels", all_kpi_labels)
    selected_kpi_labels = st.sidebar.multiselect(
        "KPI tiles",
        all_kpi_labels,
        default=default_kpi_labels,
        key="selected_kpi_labels_widget",
    )
    if selected_kpi_labels != default_kpi_labels:
        st.session_state["selected_kpi_labels"] = selected_kpi_labels
    _persist_selected_fields(store, profile.selected_race)
    st.session_state["selected_kpi_labels"] = selected_kpi_labels

    min_d, default_since, max_d = period_bounds([a.date for a in activities])
    period = st.sidebar.date_input(
        "Period",
        value=(default_since, max_d),
        min_value=min_d,
        max_value=max_d,
        key=PERIOD_KEY,
    )
    st.sidebar.caption(f"Store range {min_d} → {max_d} · {len(activities)} activities")
    if isinstance(period, (tuple, list)):
        since, until = period
    else:
        since = until = period

    compute_sig = (
        activities_sig(activities),
        (profile.hrmax, profile.hrrest, profile.sex, profile.birth_year, profile.hrmax_source),
        st.session_state.get("lt_payload"),
        st.session_state.get("race_payload"),
        st.session_state.get("vo2max_payload"),
        st.session_state.get("series_by_id"),
    )
    if st.session_state.get("_view_sig") != compute_sig:
        st.session_state["view"] = build_session_view(
            activities,
            profile,
            st.session_state.get("lt_payload"),
            st.session_state.get("race_payload"),
            st.session_state.get("vo2max_payload"),
            st.session_state.get("series_by_id"),
        )
        st.session_state["_view_sig"] = compute_sig
        # Mirror the computed rows (including ingested race predictions) into
        # the store so direct DB readers see what the UI shows.
        persist_session_metrics(
            store,
            activities,
            profile,
            st.session_state.get("lt_payload"),
            st.session_state.get("race_payload"),
            st.session_state.get("vo2max_payload"),
            st.session_state.get("series_by_id"),
        )
    view = st.session_state["view"]
    windowed = view.windowed(since, until)

    st.subheader("Current values (within selected range)")
    selected_kpis = st.session_state.get("selected_kpi_labels")
    render_kpis(windowed, view, units, selected_kpis)

    tab_load, tab_fitness, tab_volume, tab_acts = st.tabs(
        ["Load & Recovery", "Fitness", "Volume & Terrain", "Activities"]
    )
    with tab_load:
        render_load_tab(view, windowed, units)
    with tab_fitness:
        render_fitness_tab(view, windowed, units, profile.selected_race)
    with tab_volume:
        render_volume_tab(activities, view, windowed, units)
    with tab_acts:
        render_activities(activities, since, until, units)

    with st.expander("How to read this dashboard"):
        for line in GLOSSARY:
            st.markdown(f"- {line}")


if __name__ == "__main__":
    main()
