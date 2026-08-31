"""Training Tracker — interactive dashboard (Streamlit).

Run from v2/:

    ../.venv/bin/streamlit run dashboard.py

Reads a persistent SQLite history DB (activities + runner profile), fetches
fresh data from Garmin on demand, and renders every emitted metric with
interpretation context. All computation lives in session.build_session_view;
this module is a thin view.
"""
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from gateway import GarminGateway
from normalize import from_summary
from profile import RunnerProfile, default_profile, with_estimated_hrmax
from session import _fmt, build_session_view, run_with_timeout
from store import MetricStore

st.set_page_config(page_title="Training Tracker", layout="wide")

DB_PATH = os.environ.get("TRAINING_DB", str(Path(__file__).parent / "data" / "training.sqlite"))
FETCH_DAYS = 365
FETCH_TIMEOUT = 90  # hard cap on the whole Garmin refresh (auth + fetches)
DEFAULT_WINDOW_DAYS = 180

KPI_KEYS = [
    ("load.acwr", "ACWR"),
    ("load.acwr_pct", "ACWR %"),
    ("pmc.ctl", "CTL"),
    ("pmc.atl", "ATL"),
    ("pmc.tsb", "TSB"),
    ("volume.distance_total", "Weekly"),
    ("fitness.vo2max", "VO2max"),
    ("load.lt_hr", "LT HR"),
]

CROSS_CHARTS = [
    ("load.banister_cross", "Cross-training TRIMP (Banister)"),
    ("load.edwards_cross", "Cross-training TRIMP (Edwards)"),
]

GLOSSARY = [
    "Prefer TREND over absolute value for every metric.",
    "Banister TRIMP = minutes * dHR * 0.64 * exp(b*dHR); b = 1.92 (M) / 1.67 (F). "
    "Raising the chosen HRmax lowers every TRIMP value.",
    "CTL (tau 42d) = fitness; ATL (tau 7d) = fatigue; TSB = CTL - ATL, positive = form.",
    "ACWR = mean daily load (7d) / mean daily load (28d), coupled. It measures load "
    "SWING, not injury prediction; the green 0.8-1.3 band is a heuristic.",
    "VO2max is a Firstbeat estimate (~5% error, underestimates >=60 mL/kg/min); never "
    "recomputed here.",
    "LT heart rate anchors (~7% error); LT pace can overestimate 20-26%.",
    "Race predictions: 5K/10K/half are the trustworthy end; marathon least.",
    "Cross-training TRIMP is HR/time/calories only and never feeds the running "
    "PMC/ACWR windows.",
    "Elevation is route context, not a risk metric.",
    "Garmin proprietary load/TE and ACWR bands are reference, never gates.",
    "Aerobic decoupling needs >=6 route-matched flat sessions; single-run values are "
    "noise and are not shown here.",
]


@st.cache_resource
def get_store() -> MetricStore:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return MetricStore(DB_PATH)


def refresh_garmin() -> tuple[list, dict, dict]:
    gw = GarminGateway(cache_dir=Path("cache/app_cache"))
    end = date.today()
    start = end - timedelta(days=FETCH_DAYS)
    with st.spinner("Fetching activities from Garmin..."):
        raw = gw.fetch_activities(start.isoformat(), end.isoformat())
    if not raw:
        raise RuntimeError("Garmin returned no activities")
    acts = [from_summary(a) for a in raw]
    lt = gw.fetch_lactate_threshold() or {}
    race = gw.fetch_race_predictions() or {}
    return acts, lt, race


def profile_from_widgets(activities, persisted: RunnerProfile | None = None) -> RunnerProfile:
    p = persisted or default_profile()
    sex = st.sidebar.selectbox("Sex", ("M", "F"), index=0 if p.sex == "M" else 1)
    birth_year = st.sidebar.number_input("Birth year", min_value=1920,
                                         max_value=2100, value=p.birth_year)
    hrrest = st.sidebar.number_input("Resting HR (bpm)", min_value=30,
                                     max_value=120, value=p.hrrest)
    src = st.sidebar.radio("HRmax source",
                           ("manual", "estimate from workouts", "age-predicted"),
                           index=0 if p.hrmax_source == "configured" else 2)
    if src == "manual":
        default_hrmax = p.hrmax if p.hrmax_source == "configured" else 220 - (date.today().year - int(birth_year))
        hrmax = st.sidebar.number_input("HRmax (bpm)", min_value=110, max_value=240,
                                        value=default_hrmax)
        return RunnerProfile(hrmax=int(hrmax), hrrest=int(hrrest), sex=sex,
                             birth_year=int(birth_year), hrmax_source="configured")
    base = RunnerProfile.from_age(age=date.today().year - int(birth_year),
                                  hrrest=int(hrrest), sex=sex,
                                  birth_year=int(birth_year))
    if src == "estimate from workouts":
        return with_estimated_hrmax(base, activities)
    return base


def profile_sig(p: RunnerProfile) -> tuple:
    return (p.hrmax, p.hrrest, p.sex, p.birth_year, p.hrmax_source, p.units)


def activities_sig(acts) -> tuple:
    return tuple(
        (a.activity_id, a.sport, a.date.isoformat(), a.distance_m, a.duration_s,
         a.avg_hr, a.max_hr, tuple(sorted(a.zone_s.items())))
        for a in acts
    )


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


def render_kpis(windowed, view, units) -> None:
    cols = st.columns(len(KPI_KEYS))
    for col, (key, label) in zip(cols, KPI_KEYS):
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
    s = windowed.get("load.banister")
    if s is None or s.empty:
        st.write("No outdoor-running TRIMP data in this window.")
    else:
        show_ed = st.checkbox("Overlay Edwards TRIMP")
        fig = go.Figure()
        fig.add_trace(go.Bar(x=s.index, y=s.values, name="Banister TRIMP"))
        if show_ed:
            e = windowed.get("load.edwards")
            if e is not None and len(e):
                fig.add_trace(go.Scatter(x=e.index, y=e.values, name="Edwards TRIMP",
                                         yaxis="y2"))
                fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                              title="Edwards TRIMP"))
        fig.update_layout(title="Daily TRIMP (outdoor running)",
                          hovermode="x unified",
                          legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Banister TRIMP scales DOWN when the chosen HRmax is raised. "
                   + context_line(view, "load.banister"))

    tsb = windowed.get("pmc.tsb")
    if tsb is None or tsb.empty:
        st.write("PM C chart needs load.banister history in this window.")
    else:
        fig = go.Figure()
        for key, name, color in (("pmc.ctl", "CTL", "#2E86AB"),
                                 ("pmc.atl", "ATL", "#A23B72")):
            t = windowed.get(key)
            if t is not None and len(t):
                fig.add_trace(go.Scatter(x=t.index, y=t.values, name=name,
                                         line=dict(color=color)))
        fig.add_trace(go.Scatter(x=tsb.index, y=tsb.values, name="TSB",
                                 yaxis="y2", line=dict(color="#F18F01")))
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        fig.update_layout(title="Fitness (CTL) / Fatigue (ATL) / Form (TSB)",
                          hovermode="x unified", yaxis_title="TRIMP / day",
                          yaxis2=dict(overlaying="y", side="right", title="TSB"),
                          legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("TSB = CTL - ATL; positive = fresh/form. Windows tau 42/7 days.")

    acwr = windowed.get("load.acwr")
    if acwr is None or acwr.empty:
        st.write("ACWR needs a >=28-day outdoor-running span to build the chronic "
                 "window.")
    else:
        fig = go.Figure()
        fig.add_hrect(y0=0.8, y1=1.3, fillcolor="lightgreen", opacity=0.2,
                      line_width=0)
        fig.add_trace(go.Scatter(x=acwr.index, y=acwr.values, name="ACWR",
                                 line=dict(color="#2E86AB")))
        pct = windowed.get("load.acwr_pct")
        if pct is not None and len(pct):
            fig.add_trace(go.Scatter(x=pct.index, y=pct.values * 100,
                                     name="history pct (%)", yaxis="y2",
                                     line=dict(color="#F18F01", dash="dash")))
            fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                          title="% within last 180d"))
        fig.add_hline(y=0.5, line_dash="dot", line_color="red")
        fig.add_hline(y=1.5, line_dash="dot", line_color="red")
        fig.update_layout(title="Acute:Chronic Workload Ratio (coupled 7/28)",
                          hovermode="x unified",
                          yaxis=dict(range=[0, max(2.0, float(acwr.max()))]),
                          legend=dict(orientation="h", y=1.12))
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Green 0.8-1.3 is a heuristic. ACWR measures load SWING, not "
                   "injury prediction. " + context_line(view, "load.acwr"))

    cross = [(k, name) for k, name in CROSS_CHARTS
             if (windowed.get(k) is not None and not windowed.get(k).empty)]
    if cross:
        fig = go.Figure()
        for key, name in cross:
            t = windowed[key]
            fig.add_trace(go.Bar(x=t.index, y=t.values, name=name))
        fig.update_layout(title="Cross-training TRIMP (HR only, excluded from "
                                "running PMC/ACWR)", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("basis=cross_training — never feeds the running-anchored "
                   "PMC/ACWR windows.")
    elif view.context.get("load.banister_cross"):
        st.write("No cross-training TRIMP in this window.")
    else:
        st.write("Cross-training TRIMP requires activities with HR data.")


def render_fitness_tab(view, windowed, units) -> None:
    vo2 = windowed.get("fitness.vo2max")
    if vo2 is None or vo2.empty:
        st.write("No VO2max estimates (needs per-run vO2MaxValue on running "
                 "activities).")
    else:
        fig = go.Figure(go.Scatter(x=vo2.index, y=vo2.values, mode="lines+markers",
                                   name="VO2max"))
        fig.update_layout(title="VO2max (Firstbeat estimate)", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Firstbeat estimate ~5% error, underestimates >=60 mL/kg/min. "
                   + context_line(view, "fitness.vo2max"))

    threshes = [("load.lt_hr", "LT heart rate", {"unit": "bpm"}),
                ("load.lt_pace", "LT pace", None),
                ("load.cs_approx", "Approx critical speed (fastest mile)", None)]
    for key, name, params in threshes:
        s = windowed.get(key)
        if s is None or s.empty:
            continue
        y = s.values if params else pace_per_unit(s, units)
        labels = [_fmt(v, params or {"unit": "s"}, units) for v in y]
        fig = go.Figure(go.Scatter(x=s.index, y=y, mode="lines+markers",
                                   name=name, text=labels,
                                   hovertemplate="%{x}<br>%{text}"))
        fig.update_layout(title=name, yaxis_title=("bpm" if params else
                                                   f"sec per {units}"),
                          hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(context_line(view, key))
    if not windowed.get("load.lt_hr") and not windowed.get("load.lt_pace"):
        st.write("LT heart rate / pace require a measured lactate threshold in "
                 "the Garmin payload (appears after a Refresh).")

    preds = {d: windowed.get(f"race_{d}") for d in ("5k", "10k", "half", "full")}
    if any(p is not None and len(p) for p in preds.values()):
        fig = go.Figure()
        for dist, s in preds.items():
            if s is None or len(s) == 0:
                continue
            labels = [_fmt(v, {"unit": "s"}, units) for v in s.values]
            fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines+markers",
                                     line_shape="hv", name=f"{dist} race",
                                     text=labels,
                                     hovertemplate="%{x}<br>%{text}"))
        fig.update_layout(title="Garmin race predictions", hovermode="x unified",
                          yaxis_title="seconds")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("5K/10K/half are the trustworthy end; marathon is the least "
                   "trustworthy prediction.")
    else:
        st.write("Race predictions appear after a Refresh (requires new Garmin "
                 "predictions in the payload).")


def render_volume_tab(activities, view, windowed, units) -> None:
    weeks = {g: windowed.get(f"volume.distance_{g}") for g in
             ("total", "running", "treadmill", "cross")}
    weeks = {g: s for g, s in weeks.items() if s is not None and len(s)}
    if weeks:
        idx = pd.Index([])
        for s in weeks.values():
            idx = idx.union(s.index)
        fig = go.Figure()
        for g, s in weeks.items():
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=g))
        roll = windowed.get("volume.rolling4wk_total")
        if roll is not None and len(roll):
            fig.add_trace(go.Scatter(x=roll.index, y=roll.values,
                                     name="rolling 4wk", yaxis="y2",
                                     line=dict(color="#F18F01")))
            fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                          title=f"rolling {'mi' if units in ('miles', 'imperial') else 'km'}"))
        fig.update_layout(title="Weekly volume", barmode="group",
                          hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        st.caption(context_line(view, "volume.distance_total"))
    else:
        st.write("No volume data in this window.")

    wow = windowed.get("volume.wow_pct_total")
    if wow is not None and len(wow):
        fig = go.Figure(go.Bar(x=wow.index, y=wow.values * 100, name="week-over-week %"))
        fig.add_hline(y=0, line_dash="dot", line_color="gray")
        fig.update_layout(title="Week-over-week volume change", hovermode="x unified",
                          yaxis_title="%")
        st.plotly_chart(fig, use_container_width=True)

    gain = windowed.get("elevation.daily_gain_running")
    if gain is not None and len(gain):
        fig = go.Figure(go.Bar(x=gain.index, y=gain.values,
                               name=f"daily gain ({'ft' if units in ('miles', 'imperial') else 'm'})"))
        roll28 = windowed.get("elevation.rolling28d_running")
        if roll28 is not None and len(roll28):
            fig.add_trace(go.Scatter(x=roll28.index, y=roll28.values,
                                     name="rolling 28d", line=dict(color="#F18F01")))
        per_km = windowed.get("elevation.gain_per_km_running")
        if per_km is not None and len(per_km):
            fig.add_trace(go.Scatter(x=per_km.index, y=per_km.values,
                                     name=f"{'ft' if units in ('miles', 'imperial') else 'm'} / {'mi' if units in ('miles', 'imperial') else 'km'}",
                                     yaxis="y2",
                                     line=dict(color="#2E86AB", dash="dash")))
            fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                          title=f"{'ft' if units in ('miles', 'imperial') else 'm'} / {'mi' if units in ('miles', 'imperial') else 'km'}"))
        fig.update_layout(title="Elevation gain (running)", hovermode="x unified")
        st.plotly_chart(fig, use_container_width=True)
        st.caption("Elevation is route context, not a risk metric.")
    else:
        st.write("No elevation data in this window.")


def render_activities(activities, since, until, units) -> None:
    rows = []
    for a in activities:
        if not (since <= a.date <= until):
            continue
        pace = None
        if a.avg_speed:
            m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
            pace = m_per_unit / a.avg_speed
        rows.append({
            "date": a.date.isoformat(),
            "sport": a.sport,
            "distance": _fmt(a.distance_km, {"unit": "km"}, units),
            "duration": _fmt(a.duration_s, {"unit": "s"}, units),
            "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
            "avg HR": f"{a.avg_hr:.0f}" if a.avg_hr is not None else "—",
            "max HR": f"{a.max_hr:.0f}" if a.max_hr is not None else "—",
            "VO2max": f"{a.vo2max:.1f}" if a.vo2max is not None else "—",
            "elev gain": _fmt(a.ele_gain_m, {"unit": "m"}, units) if a.ele_gain_m is not None else "—",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        st.write("No activities in this window.")
        return
    st.dataframe(df.sort_values("date", ascending=False),
                 use_container_width=True, hide_index=True)


def main() -> None:
    st.title("Training Tracker")
    store = get_store()

    persisted_profile = store.load_runner_profile()
    _units_display = "km" if (persisted_profile or default_profile()).units == "metric" else "miles"
    units = st.sidebar.radio("Units", ("km", "miles"),
                             index=0 if _units_display == "km" else 1)

    if "activities" not in st.session_state:
        st.session_state["activities"] = store.load_activities()
        st.session_state["lt_payload"] = None
        st.session_state["race_payload"] = None

    st.sidebar.header("Data")
    if st.sidebar.button("Refresh from Garmin"):
        try:
            acts, lt, race = run_with_timeout(refresh_garmin, timeout=FETCH_TIMEOUT)
            store.save_activities(acts)
            st.session_state["activities"] = acts
            st.session_state["lt_payload"] = lt
            st.session_state["race_payload"] = race
            st.sidebar.success(f"Fetched {len(acts)} activities")
        except Exception as exc:
            st.sidebar.warning(f"Garmin fetch failed: "
                               f"{exc.__class__.__name__}: {exc}")

    activities = st.session_state["activities"]
    if not activities:
        st.info("No data yet. Click **Refresh from Garmin** in the sidebar to "
                "pull your activity history into the local database, then set "
                "your profile inputs. Everything stays local except that initial "
                "Garmin pull.")
        st.stop()

    st.sidebar.header("Profile")
    units_stored = "metric" if units == "km" else "imperial"
    profile = replace(profile_from_widgets(activities, persisted_profile), units=units_stored)
    psig = profile_sig(profile)
    if st.session_state.get("_profile_sig") != psig:
        store.save_runner_profile(profile)
        st.session_state["_profile_sig"] = psig

    min_d = min(a.date for a in activities)
    max_d = max(a.date for a in activities)
    default_since = max(min_d, max_d - timedelta(days=DEFAULT_WINDOW_DAYS))
    period = st.sidebar.date_input("Period", value=(default_since, max_d),
                                   min_value=min_d, max_value=max_d)
    if isinstance(period, (tuple, list)):
        since, until = period
    else:
        since = until = period

    compute_sig = (activities_sig(activities),
                   (profile.hrmax, profile.hrrest, profile.sex, profile.birth_year, profile.hrmax_source),
                   st.session_state.get("lt_payload"), st.session_state.get("race_payload"))
    if st.session_state.get("_view_sig") != compute_sig:
        st.session_state["view"] = build_session_view(
            activities, profile,
            st.session_state.get("lt_payload"), st.session_state.get("race_payload"))
        st.session_state["_view_sig"] = compute_sig
    view = st.session_state["view"]
    windowed = view.windowed(since, until)

    st.subheader("Current values (within selected range)")
    render_kpis(windowed, view, units)

    tab_load, tab_fitness, tab_volume, tab_acts = st.tabs(
        ["Load & Recovery", "Fitness", "Volume & Terrain", "Activities"])
    with tab_load:
        render_load_tab(view, windowed, units)
    with tab_fitness:
        render_fitness_tab(view, windowed, units)
    with tab_volume:
        render_volume_tab(activities, view, windowed, units)
    with tab_acts:
        render_activities(activities, since, until, units)

    with st.expander("How to read this dashboard"):
        for line in GLOSSARY:
            st.markdown(f"- {line}")


if __name__ == "__main__":
    main()
