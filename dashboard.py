"""Training Tracker — interactive dashboard (Streamlit).

Run from root:

    .venv/bin/streamlit run dashboard.py

Reads a persistent SQLite history DB (activities + runner profile), fetches
fresh data from Garmin on demand, and renders every emitted metric with
interpretation context. All computation lives in session.build_session_view;
this module is a thin view.
"""

import json
import sys
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from profile import RunnerProfile, default_profile, with_estimated_hrmax

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from chart_theme import PALETTE, render, style
from gateway import GarminGateway, compute_fetch_start
from metrics import daniels_gilbert, marathon_shape, volume
from metrics.gap import K as GAP_K
from metrics.threshold import parse_details_series
from normalize import from_summary, parse_laps, parse_rpe_feel, parse_weather
from pipeline import persist_session_metrics
from session import (
    _fmt,
    activity_graded_pct,
    best_mile,
    build_session_view,
    fastest_30s_pace,
    partial_bucket_labels,
    resolve_activities,
    run_with_timeout,
)
from store import MetricStore, default_db_path

st.set_page_config(page_title="Training Tracker", layout="wide")

DB_PATH = str(default_db_path())
FETCH_DAYS = 365
FETCH_TIMEOUT = 90  # hard cap on the whole Garmin refresh (auth + fetches)
BACKFILL_CALL_TIMEOUT_S = 8  # per summary/weather fetch during RPE/weather backfill
BACKFILL_RESERVE_S = 15  # stop RPE/weather backfill this many seconds before FETCH_TIMEOUT
# The persisted RPE/Feel/weather columns (Task 2 schema) — the catch-up pass
# selects stored rows still holding NULLs among these.
_RPE_WX_FIELDS = (
    "rpe_10",
    "feel",
    "wx_temp",
    "wx_feels",
    "wx_humidity",
    "wx_wind",
    "wx_wind_dir",
    "wx_desc",
    "wx_unit",
)
DEFAULT_WINDOW_DAYS = 180
PERIOD_KEY = "period"
CACHE_DIR_NAME = "app_cache"
APP_CACHE_DIR = Path(__file__).resolve().parent / "cache" / CACHE_DIR_NAME
LOGS_DIR = Path(__file__).resolve().parent / "logs"
HEALTH_LOG = LOGS_DIR / "health.log"

KPI_KEYS = [
    ("load.acwr", "ACWR"),
    ("pmc.ctl", "CTL"),
    ("pmc.atl", "ATL"),
    ("pmc.tsb", "TSB"),
    ("volume.distance_total", "Weekly"),
    ("fitness.vo2max", "VO2max"),
    ("load.lt_hr", "LT HR"),
    ("load.banister", "Daily load"),
    ("load.decoupling_mean", "Aerobic decoupling"),
]

KPI_INTERPRETATIONS = {
    "load.acwr": "Acute:Chronic Workload Ratio (7-day / 28-day). >1.0 = load increasing, <1.0 = load decreasing. Green zone 0.8–1.3 is a heuristic.",
    "pmc.ctl": "Chronic Training Load (fitness). 42-day exponential average of daily TRIMP. Higher = more fitness.",
    "pmc.atl": "Acute Training Load (fatigue). 7-day exponential average of daily TRIMP. Higher = more fatigue.",
    "pmc.tsb": "Training Stress Balance = CTL − ATL. Positive = fresh/form (ready to race); negative = tired.",
    "volume.distance_total": "Weekly running + treadmill distance. Stacked by sport in Volume tab.",
    "fitness.vo2max": "Garmin Firstbeat VO2max estimate (mL/kg/min). ~5% error; underestimates at high levels.",
    "load.lt_hr": "Lactate threshold heart rate from Garmin (bpm). ~7% error. Anchors training zones.",
    "load.banister": "Daily Banister TRIMP (combined running + treadmill + cross-training HR load). Feeds PMC/ACWR.",
    "load.decoupling_mean": "Mean aerobic decoupling across ≥6 route-matched 90-min flat runs. Positive = cardiac drift (HR rises vs pace).",
}

# KPI fallback sources: primary -> (fallback key, label suffix). Used when the
# primary series is empty in the selected window. The suffix keeps the tile
# honest about provenance (measured Garmin record vs computed estimate).
KPI_FALLBACKS = {
    "load.lt_hr": ("load.lt_hr_best20", " (best-effort)"),
}


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


def reset_period_after_refresh(old_period, default_since: date, max_d: date) -> tuple[date, date]:
    """Period to show after a Garmin refresh added activities.

    Preserves the user's existing start date and pushes the end out to the new
    latest activity date, so freshly fetched activities appear in every tab.
    Falls back to the trailing-window default when there is no prior selection.
    A single-date selection is treated as start == end == that date.
    """

    def _norm(v):
        return v.date() if hasattr(v, "date") else v

    if isinstance(old_period, (list, tuple)) and len(old_period) == 2:
        return _norm(old_period[0]), max_d
    if old_period is not None:
        return _norm(old_period), max_d
    return default_since, max_d


@st.cache_resource
def get_store() -> MetricStore:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    return MetricStore(DB_PATH)


def _store_changed(store, session_state) -> bool:
    """True when the store has moved on from what this session loaded."""
    return store.activities_fingerprint() != session_state.get("_activities_fingerprint")


def _health_log_mtime() -> float:
    """When the health check last wrote its verdict (0.0 if it never ran)."""
    try:
        return HEALTH_LOG.stat().st_mtime
    except OSError:
        return 0.0


@st.fragment(run_every="30s")
def poll_store_changes() -> None:
    """Rerun the app when the scheduled refresh has written to the store, or
    when the health check has published a new verdict.

    Streamlit only reruns on user interaction, so an open dashboard would keep
    showing the pre-refresh activity count until someone clicked something.
    Polling the store fingerprint (columns + row count + elapsed total) is the
    cheapest signal that the refresh job changed the data.
    """
    if _store_changed(get_store(), st.session_state) or _health_log_mtime() != st.session_state.get(
        "_health_mtime"
    ):
        st.rerun(scope="app")


def health_status(log_path: Path | None = None) -> tuple[str, str] | None:
    """The outcome and time of the last health check, or None if there is none.

    The launchd health job writes its stdout to logs/health.log; the last
    "health check:" line says whether the most recent run passed. Without this
    a failing health check is invisible — nobody reads the log.
    """
    path = HEALTH_LOG if log_path is None else Path(log_path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("health check: "):
            parts = line[len("health check: ") :].split()
            if len(parts) == 3:
                return parts[2], f"{parts[0]} {parts[1]}"
    return None


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


def _persist_selected_fields(
    store: MetricStore, selected_race: str, selected_volume_agg: str
) -> None:
    """Persist user-selected race distance and volume aggregation to the runner profile."""
    profile = store.load_runner_profile()
    if profile is None:
        profile = default_profile()
    if profile.selected_race != selected_race or profile.selected_volume_agg != selected_volume_agg:
        store.save_runner_profile(
            replace(profile, selected_race=selected_race, selected_volume_agg=selected_volume_agg)
        )


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
    started = time.monotonic()  # anchors the backfill budget to the 90s guard
    _deadline = started + FETCH_TIMEOUT - BACKFILL_RESERVE_S
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
        # Incremental mode with no new activities is a normal no-op — and the
        # moment the store-driven catch-up pass does its work: rows left NULL
        # by an earlier refresh's budget stop or fetch failure resume here.
        _backfill_catchup(gw, store, _deadline)
        return [], lt, race, vo2
    acts = [from_summary(a) for a in raw]
    # Pass (a) — freshness wins: RPE/Feel/weather backfill runs BEFORE the
    # caller's store.save_activities (save_activities is INSERT OR REPLACE —
    # merging here means one save persists everything and never NULLs
    # previously backfilled rows). Bounded by the deadline so a first-run
    # backfill of hundreds of ids cannot overrun FETCH_TIMEOUT.
    _backfill_rpe_weather(gw, acts, _deadline)
    # Pass (b) — store-driven catch-up: stored rows still holding NULLs (a
    # budget stop or fetch failure on an earlier refresh) get whatever budget
    # remains; it saves its own merges, so recovery also happens on refreshes
    # with no new activities. If the budget is gone, remaining NULL rows
    # resume on the next refresh — that is the recovery mechanism.
    _backfill_catchup(gw, store, _deadline)
    return acts, lt, race, vo2


def _cached_or_fetch(gw: GarminGateway, activity_id: int, name: str, fetch) -> dict:
    """Cache-first payload load for one backfill endpoint.

    `activity_{summary,weather}_{id}.json` on disk wins (no network), so repeat
    refreshes are cheap; otherwise fetch via `run_with_timeout`.
    """
    path = gw.cache_dir / f"{name}_{activity_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    return run_with_timeout(fetch, timeout=BACKFILL_CALL_TIMEOUT_S, activity_id=activity_id)


def _needs_backfill(a) -> bool:
    """True while any persisted RPE/Feel/weather column is still NULL.

    Genuinely-absent data (indoor `{}` weather, no watch RPE) leaves columns
    NULL forever, so such rows stay candidates — but `_cached_or_fetch` then
    serves them from disk, so absent data never causes repeated *network*
    calls; only an endpoint whose fetch never succeeded (no cache file) is
    fetched again.
    """
    return any(getattr(a, f) is None for f in _RPE_WX_FIELDS)


def _backfill_activity(gw: GarminGateway, a, deadline: float) -> bool:
    """Backfill one activity, cache-first, deadline-checked between endpoints.

    Returns True if any backfill field changed. Each endpoint's fetch+parse+
    merge sits in its own try — a failure is logged and skipped, never raised.
    """
    aid = a.activity_id

    def _snapshot() -> tuple:
        return tuple(getattr(a, f) for f in _RPE_WX_FIELDS)

    before = _snapshot()
    try:
        summary = _cached_or_fetch(gw, aid, "activity_summary", gw.fetch_activity_summary)
        for key, value in parse_rpe_feel(summary).items():
            setattr(a, key, value)
    except Exception as exc:  # noqa: BLE001 - skip-on-failure: refresh must never block
        _log_backfill_skip(aid, "summary", exc)
    if time.monotonic() >= deadline:
        return _snapshot() != before  # no budget left for weather; summary counts
    try:
        weather = _cached_or_fetch(gw, aid, "activity_weather", gw.fetch_activity_weather)
        for key, value in parse_weather(weather).items():
            setattr(a, key, value)
    except Exception as exc:  # noqa: BLE001 - skip-on-failure: refresh must never block
        _log_backfill_skip(aid, "weather", exc)
    return _snapshot() != before


def _backfill_rpe_weather(gw: GarminGateway, acts: list, deadline: float) -> list:
    """Merge RPE/Feel/weather onto a list of activities.

    Shared by pass (a) (freshly fetched activities) and `_backfill_catchup`
    (pass (b): stored rows still holding NULLs). Cache-first per endpoint
    (disk payload beats network), per-call timeout via `run_with_timeout`,
    whole pass bounded by `deadline` (refresh start + FETCH_TIMEOUT -
    BACKFILL_RESERVE_S). Returns the activities whose backfill fields changed.
    Rows not reached before the deadline keep their NULLs and are resumed by
    the catch-up pass on a later refresh; any fetch/parse failure is logged
    and skipped — backfill never blocks or raises the refresh.
    """
    changed: list = []
    attempted = 0
    for a in acts:
        if time.monotonic() >= deadline:
            break
        attempted += 1
        if _backfill_activity(gw, a, deadline):
            changed.append(a)
    if attempted < len(acts):
        _log_backfill_budget(attempted, len(acts))
    return changed


def _backfill_catchup(gw: GarminGateway, store, deadline: float) -> None:
    """Store-driven catch-up pass (b): backfill stored rows still holding NULLs.

    Runs after the fresh-activity pass with whatever budget remains (freshness
    wins), over `store.load_activities()` rows selected by `_needs_backfill`.
    Same cache-first / per-call-timeout / skip-on-failure rules as pass (a).
    Only rows whose fields actually changed are written back — merges are
    persisted here because the caller only saves fresh activities. If the
    budget runs out mid-pass, the untouched rows stay NULL in the store and
    resume on the next refresh: that is the recovery mechanism.
    """
    if time.monotonic() >= deadline:
        return  # no budget left after pass (a); resume on the next refresh
    candidates = [a for a in store.load_activities() if _needs_backfill(a)]
    if not candidates:
        return
    changed = _backfill_rpe_weather(gw, candidates, deadline)
    if changed:
        store.save_activities(changed)


def _log_backfill_skip(activity_id: int, what: str, exc: Exception) -> None:
    print(
        f"[backfill] skipped {what} for activity {activity_id}: {exc.__class__.__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )


def _log_backfill_budget(done: int, total: int) -> None:
    print(
        f"[backfill] time budget exhausted after {done}/{total} rows; "
        "resumed by the next refresh's catch-up pass",
        file=sys.stderr,
        flush=True,
    )


DETAILS_TOP_K = 40
DETAILS_TIMEOUT_S = 8
DETAILS_TOTAL_TIMEOUT_S = 60
DECOUPLING_TOP_K = 20
DECOUPLING_TOTAL_TIMEOUT_S = 30


def _fetch_series_for(gw: GarminGateway, candidates: list, timeout_s: float) -> dict[int, tuple]:
    """Shared cache-first details fetch for a pre-selected candidate list.

    Returns {activity_id: (hr_list, speed_list, ts_ms_list)} for successfully
    fetched activities with a parseable series. Empty parses are skipped.
    Never blocks the refresh — skips on failure.
    """
    series_by_id: dict[int, tuple] = {}
    for a in candidates:
        cache_path = gw.cache_dir / f"activity_details_{a.activity_id}.json"
        if cache_path.exists():
            try:
                details = json.loads(cache_path.read_text())
                parsed = parse_details_series(details)
                if parsed[0]:
                    series_by_id[a.activity_id] = parsed
            except (json.JSONDecodeError, OSError):
                pass
            continue
        try:
            details = run_with_timeout(
                gw.fetch_activity_details, timeout=timeout_s, activity_id=a.activity_id
            )
            parsed = parse_details_series(details)
            if parsed[0]:
                series_by_id[a.activity_id] = parsed
        except (TimeoutError, OSError, json.JSONDecodeError, KeyError):
            pass
    return series_by_id


def fetch_details_for_decoupling(
    gw: GarminGateway,
    activities: list,
    top_k: int = DECOUPLING_TOP_K,
    timeout_s: float = DETAILS_TIMEOUT_S,
    since: "date | None" = None,
    until: "date | None" = None,
) -> dict[int, tuple]:
    """Fetch activity details for the most recent long runs (decoupling input).

    Unlike fetch_details_for_lthr (top-k fastest), this selects by duration —
    outdoor runs >= 90 min with HR, excluding structured-interval workouts —
    since slow long runs rarely make the fastest cut. Same return shape;
    callers merge both passes into one series_by_id.
    """
    candidates = [
        a
        for a in activities
        if a.sport == "running"
        and a.duration_s >= 5400
        and a.avg_hr is not None
        and not a.has_intervals
    ]
    if since is not None:
        candidates = [a for a in candidates if a.date >= since]
    if until is not None:
        candidates = [a for a in candidates if a.date <= until]
    candidates.sort(key=lambda a: a.date, reverse=True)
    return _fetch_series_for(gw, candidates[:top_k], timeout_s)


def fetch_details_for_lthr(
    gw: GarminGateway,
    activities: list,
    top_k: int = DETAILS_TOP_K,
    timeout_s: float = DETAILS_TIMEOUT_S,
    since: "date | None" = None,
    until: "date | None" = None,
) -> dict[int, tuple]:
    """Fetch activity details for the top-k outdoor running activities.

    Checks disk cache first. For cache misses, fetches with per-request timeout.
    Never blocks the refresh — skips on failure.

    When *since*/*until* are provided, candidates are filtered to that date
    range before the top-k selection, so dots reflect the fastest activities
    within the visible window rather than globally.

    Returns:
        {activity_id: (hr_list, speed_list, ts_ms_list)} for successfully
        fetched activities with a parseable series. Empty parses are skipped.
    """
    candidates = [
        a
        for a in activities
        if a.sport == "running" and a.duration_s >= 1200 and a.avg_hr is not None
    ]
    if since is not None:
        candidates = [a for a in candidates if a.date >= since]
    if until is not None:
        candidates = [a for a in candidates if a.date <= until]
    candidates.sort(key=lambda a: a.avg_speed or 0, reverse=True)
    return _fetch_series_for(gw, candidates[:top_k], timeout_s)


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
        )
    base = RunnerProfile.from_age(
        age=date.today().year - int(birth_year),
        hrrest=int(hrrest),
        sex=sex,
        birth_year=int(birth_year),
    )
    if src == "estimate from workouts":
        return replace(with_estimated_hrmax(base, activities))
    return base


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


# Runalyze's prognosis panel distance ladder [km]. Metric keys render with the
# same `:g` spelling the pipeline uses (`prognosis.1`, `prognosis.21.1`): the
# inner dot is part of the key, so keys are never split on ".".
PROGNOSIS_DISTANCES_KM = (1.0, 3.0, 5.0, 10.0, 21.1, 42.2, 50.0)

# Percentage ladder of Runalyze's tools page (`tableVo2maxPace`).
PACE_PERCENTAGES = tuple(range(65, 111, 5))


def _fmt_race_time(seconds: float | None) -> str:
    """Race time as M:SS (H:MM:SS from an hour up), '—' when absent.

    Same rendering as Runalyze's `duration` filter and as the seconds labels
    the Garmin race-prediction chart above the prognosis table uses
    (`session._fmt` with unit "s").
    """
    if seconds is None:
        return "—"
    return _fmt(seconds, {"unit": "s"}, "km")


def prognosis_rows(windowed) -> list[dict]:
    """`Distance | Raw | Damped` rows from the latest `prognosis.*` rows.

    Empty when the 5 km prognosis is absent: a date-keyed Runalyze key emits no
    row on days whose trailing window holds no qualifying run, so absence is
    normal and the block's ruled hide condition. Raw is always a column once
    rows exist; Damped is added only where damped rows exist, and a distance
    without one shows '—' rather than a filler time.
    """
    five_km = windowed.get("prognosis.5")
    if five_km is None or five_km.empty:
        return []
    show_damped = any(
        (s := windowed.get(f"prognosis.{km:g}_damped")) is not None and not s.empty
        for km in PROGNOSIS_DISTANCES_KM
    )
    rows = []
    for km in PROGNOSIS_DISTANCES_KM:
        raw = windowed.get(f"prognosis.{km:g}")
        row = {
            "Distance": f"{km:g} km",
            "Raw": _fmt_race_time(last_value(raw) if raw is not None else None),
        }
        if show_damped:
            damped = windowed.get(f"prognosis.{km:g}_damped")
            row["Damped"] = _fmt_race_time(last_value(damped) if damped is not None else None)
        rows.append(row)
    return rows


def pace_table_rows(shape: float | None, units: str = "km") -> list[dict]:
    """Training-pace rows at 65..110 % of vVO2max, derived in-session.

    `shape` is the current effective VO2max (the latest `vdot.shape` value),
    rounded first exactly as Runalyze's tools page rounds the current shape
    before the pace lookup. Empty when there is no shape, so the caller renders
    nothing.
    """
    if shape is None:
        return []
    rounded = daniels_gilbert.round_half_up(shape)
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    rows = []
    for pct in PACE_PERCENTAGES:
        seconds_per_km = daniels_gilbert.pace_s_per_km(rounded, pct / 100.0)
        rows.append(
            {
                "% vVO2max": f"{pct}%",
                "Pace": (
                    _fmt_pace_min(seconds_per_km * m_per_unit / 1000.0 / 60.0, units)
                    if seconds_per_km
                    else "—"
                ),
            }
        )
    return rows


def _fmt_rpe(rpe_10) -> str:
    """RPE on the 0–10 display scale: 6.0 → '6/10', 7.5 → '7.5/10'."""
    if rpe_10 is None:
        return "—"
    return f"{int(rpe_10)}/10" if float(rpe_10).is_integer() else f"{rpe_10:.1f}/10"


def _fmt_wx_temp(value, units: str, wx_unit: str | None = None) -> str:
    """Format a stored temperature; converts for display only, never mutates.

    Stored raw values are °F (`wx_unit` default 'F', per the parse); °C is
    shown only when the units toggle displays metric, and vice versa when a
    row somehow stores °C. Missing → '—'.
    """
    if value is None:
        return "—"
    src_c = str(wx_unit or "F").upper() == "C"
    metric = units in ("km", "metric")
    if metric and not src_c:
        value = (value - 32.0) * 5.0 / 9.0
    elif not metric and src_c:
        value = value * 9.0 / 5.0 + 32.0
    return f"{value:.0f}°{'C' if metric else 'F'}"


def _fmt_wx_hum(value) -> str:
    """Relative humidity as a whole percentage; missing → '—'."""
    return f"{value:.0f}%" if value is not None else "—"


def _fmt_wx_wind(value, units: str, wind_dir: str | None = None) -> str:
    """Format stored wind speed (raw mph from the US weather station).

    Shown in km/h only when the units toggle displays metric (same display-only
    rule as temperature); direction rides along when recorded. Missing → '—'.
    """
    if value is None:
        return "—"
    text = f"{value * 1.609344:.0f} km/h" if units in ("km", "metric") else f"{value:.0f} mph"
    return f"{text} {wind_dir}" if wind_dir else text


def _rpe_wx_cols(a, units: str) -> dict:
    """RPE/Feel/weather display columns shared by the flat activities list and
    the repetition-comparison table. Pure formatting over stored values (never
    a fetch — older rows degrade to '—' until a refresh backfills them)."""
    return {
        "RPE": _fmt_rpe(a.rpe_10),
        "Feel": a.feel or "—",
        "Temp": _fmt_wx_temp(a.wx_temp, units, a.wx_unit),
        "Hum": _fmt_wx_hum(a.wx_humidity),
        "Wind": _fmt_wx_wind(a.wx_wind, units, a.wx_wind_dir),
        "Wx": a.wx_desc or "—",
    }


def anchor_point(view, hr_key: str, pace_key: str) -> dict | None:
    """Latest best-effort anchor from the UNFILTERED view series.

    Anchors are single all-time-best points; period-windowing would drop
    them whenever the best predates the selected window, so cards always
    read the full series. Returns {proxy_hr, raw_hr, pace, date} or None.
    """
    hr_s = view.series.get(hr_key)
    if hr_s is None or hr_s.empty:
        return None
    proxy_hr = float(hr_s.iloc[-1])
    meta = view.context.get(hr_key, {})
    factor = meta.get("params", {}).get("factor")
    raw_hr = proxy_hr / factor if factor and factor != 0 else proxy_hr
    pace_s = view.series.get(pace_key)
    speed = float(pace_s.iloc[-1]) if pace_s is not None and not pace_s.empty else None
    dt = hr_s.index[-1]
    return {
        "proxy_hr": proxy_hr,
        "raw_hr": raw_hr,
        "speed_m_s": speed,
        "date": dt.strftime("%Y-%m-%d") if hasattr(dt, "strftime") else str(dt),
    }


def _anchor_cards(windowed, view, units) -> None:
    """Render best-effort LTHR anchor cards with qualifier dot scatters.

    Cards read the unfiltered view (anchors are all-time bests); dots read
    the period-windowed series (in-window qualifier context).
    """
    st.markdown(
        """
        **LTHR anchors (best-effort)** — When Garmin's lactate threshold is missing, these are your
        best 20-min and 30-min sustained efforts (outdoor running, HR + pace).
        The dots below show all qualifying efforts in the current window for context.
        """
    )
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
        pt = anchor_point(view, hr_key, pace_key)
        if pt is None:
            continue
        has_any = True
        if pt["speed_m_s"]:
            m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
            pace_str = _fmt_pace_min((m_per_unit / pt["speed_m_s"]) / 60.0, units)
        else:
            pace_str = "—"

        card_text = f"{pt['proxy_hr']:.0f} bpm ({pt['raw_hr']:.0f} raw @ {pace_str}, {pt['date']})"
        st.metric(f"LTHR anchor {label}", card_text)
        st.caption(
            f"📊 **How to read:** Your best {label} sustained effort. Proxy HR = adjusted for HRmax source. "
            f"Raw HR = actual recorded. Pace = average for that effort. Date = when it occurred."
        )

        # Qualifier dots: faint scatter of all qualifying efforts.
        # (Interval/sustained encoding was tried and reverted: the device
        # flag marks every structured workout including easy foundation
        # runs, and window-speed variability doesn't separate them either.)
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
            style(
                fig,
                title=f"Qualifier dots — {label} window (all qualifying efforts)",
                yaxis_title="bpm",
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"min per {units}",
                },
                hovermode="x unified",
                showlegend=False,
            )
            render(fig, width='stretch')
            st.caption(
                f"📊 **How to read:** Faint dots = all {label} qualifying efforts in the current window. "
                f"Left axis (blue) = HR. Right axis (orange) = pace. "
                f"Cluster = consistency. Outliers = anomalies. " + context_line(view, dots_hr_key)
            )

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


def render_kpis(windowed, view, units, selected_kpi_labels: list[str] | None = None) -> None:
    visible = [
        (k, lbl) for k, lbl in KPI_KEYS if selected_kpi_labels is None or lbl in selected_kpi_labels
    ]
    cols = st.columns(len(visible))
    for col, (key, label) in zip(cols, visible, strict=False):
        s = windowed.get(key)
        val = last_value(s) if s is not None else None
        if val is None and key in KPI_FALLBACKS:
            fb_key, suffix = KPI_FALLBACKS[key]
            fb_s = windowed.get(fb_key)
            fb_val = last_value(fb_s) if fb_s is not None else None
            if fb_val is not None:
                key, label, val = fb_key, label + suffix, fb_val
        if val is None:
            col.metric(label, "—")
            col.caption(KPI_INTERPRETATIONS.get(key, ""))
            continue
        if key == "volume.distance_total":
            label = f"Weekly {'mi' if units in ('miles', 'imperial') else 'km'}"
        meta = (view.context.get(key) or {}).get("params", {})
        col.metric(label, _fmt(val, meta, units))
        interpretation = KPI_INTERPRETATIONS.get(key)
        if interpretation:
            col.caption(interpretation)


def render_load_tab(view, windowed, units) -> None:
    st.markdown(
        """
        **Load & Recovery** — How hard you've trained and how recovered you are.

        - **Daily TRIMP** = Banister heart-rate load per day (running + treadmill + cross-training with HR). Stacked by sport.
        - **PMC (CTL/ATL/TSB)** = Fitness (42-day avg), Fatigue (7-day avg), and Form (CTL − ATL) from combined TRIMP.
        - **ACWR** = Acute (7-day) / Chronic (28-day) load ratio. Measures load *swing*, not injury risk.
        - **Longest Safe Run** = Today's longest run vs. your 30-day max. Ratios > 1.1 = elevated injury risk.
        """
    )

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
                style(fig, yaxis2={"overlaying": "y", "side": "right", "title": "Edwards TRIMP"})
        style(
            fig,
            barmode="stack",
            title="Daily Banister TRIMP (stacked by sport)",
            hovermode="x unified",
            yaxis_title="Banister TRIMP",
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Each bar = one day's HR-based training load. Stacked = outdoor running (blue) + "
            "treadmill (orange) + cross-training (green). The sum feeds the PMC/ACWR charts below. "
            "Cross-training uses HR intensity only (no distance). "
            + context_line(view, "load.banister")
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
        for key, name, color in (
            ("pmc.ctl", "CTL (fitness)", PALETTE["blue"]),
            ("pmc.atl", "ATL (fatigue)", PALETTE["magenta"]),
        ):
            t = windowed.get(key)
            if t is not None and len(t):
                fig.add_trace(go.Scatter(x=t.index, y=t.values, name=name, line={"color": color}))
        fig.add_trace(
            go.Scatter(
                x=tsb.index,
                y=tsb.values,
                name="TSB (form)",
                yaxis="y2",
                line={"color": PALETTE["orange"]},
            )
        )
        fig.add_hline(y=0, line_dash="dot", line_color=PALETTE["gray"])
        style(
            fig,
            title="Fitness (CTL) / Fatigue (ATL) / Form (TSB)",
            hovermode="x unified",
            yaxis_title="Combined TRIMP / day",
            yaxis2={"overlaying": "y", "side": "right", "title": "TSB"},
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Blue = CTL (42-day rolling fitness). Purple = ATL (7-day rolling fatigue). "
            "Orange = TSB = CTL − ATL. **Above zero = fresh/form (good for racing). Below zero = tired.** "
            "CTL rises slowly with consistent training; ATL spikes with hard weeks. "
            + context_line(view, "pmc.ctl")
        )

    acwr = windowed.get("load.acwr")
    if acwr is None or acwr.empty:
        st.write("ACWR needs a >=28-day combined-load span to build the chronic window.")
    else:
        fig = go.Figure()
        fig.add_hrect(y0=0.8, y1=1.3, fillcolor=PALETTE["safe"], opacity=0.2, line_width=0)
        fig.add_trace(
            go.Scatter(x=acwr.index, y=acwr.values, name="ACWR", line={"color": PALETTE["blue"]})
        )
        fig.add_hline(y=0.5, line_dash="dot", line_color=PALETTE["danger"])
        fig.add_hline(y=1.5, line_dash="dot", line_color=PALETTE["danger"])
        style(
            fig,
            title="Acute:Chronic Workload Ratio (coupled 7/28)",
            hovermode="x unified",
            yaxis={"range": [0, max(2.0, float(acwr.max()))]},
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Blue line = ACWR (7-day avg load ÷ 28-day avg load). **1.0 = steady load.** "
            "Green band 0.8–1.3 = typical 'sweet spot'. **ACWR measures load swing, not injury prediction.** "
            + context_line(view, "load.acwr")
        )

    max_ratio = windowed.get("injury.max_run_ratio")
    if max_ratio is not None and not max_ratio.empty:
        fig = go.Figure()
        fig.add_hrect(y0=0.0, y1=1.1, fillcolor=PALETTE["safe"], opacity=0.15, line_width=0)
        fig.add_hrect(y0=1.1, y1=1.3, fillcolor=PALETTE["caution"], opacity=0.15, line_width=0)
        fig.add_hrect(y0=1.3, y1=3.0, fillcolor=PALETTE["danger"], opacity=0.1, line_width=0)
        fig.add_trace(
            go.Scatter(
                x=max_ratio.index,
                y=max_ratio.values,
                name="Max run ratio",
                mode="lines+markers",
                line={"color": PALETTE["blue"]},
            )
        )
        fig.add_hline(
            y=1.1, line_dash="dash", line_color=PALETTE["caution"], annotation_text="110%"
        )
        fig.add_hline(y=1.3, line_dash="dash", line_color=PALETTE["danger"], annotation_text="130%")
        style(
            fig,
            title="Longest Safe Run — single run vs rolling 30-day max",
            hovermode="x unified",
            yaxis={"title": "Ratio", "range": [0, max(2.0, float(max_ratio.max()) * 1.1)]},
            xaxis_title="Date",
        )
        render(fig, width='stretch')
        latest = max_ratio.iloc[-1]
        if latest > 1.3:
            st.error(
                f"Latest run is {latest:.0%} of your 30-day longest — **high injury risk**. "
                "Consider scaling back."
            )
        elif latest > 1.1:
            st.warning(
                f"Latest run is {latest:.0%} of your 30-day longest — **elevated risk**. "
                "Keep an eye on cumulative load."
            )
        else:
            st.success(f"Latest run is {latest:.0%} of your 30-day longest — within safe range.")
        st.caption(
            "📊 **How to read:** Ratio = today's longest run distance ÷ your longest run in the past 30 days "
            "(running + treadmill). **Green < 110% = safe. Orange 110–130% = elevated risk. Red > 130% = high risk.** "
            "Study found 64% injury risk increase above 110%, roughly doubling at 2x. "
            + context_line(view, "injury.max_run_ratio")
        )


# The only prompt telling the user that Garmin VO2max is unsynced. Shown when the
# Garmin series is absent — including when `vdot.shape` alone exists, so the
# Runalyze overlay never silently hides it.
GARMIN_VO2MAX_HINT = (
    "No VO2max trend data. Use **Refresh from Garmin** to pull the "
    "daily trend from `/maxmet/daily` (Garmin running VO2max)."
)


def render_fitness_tab(view, windowed, units, selected_race: str = "5k") -> None:
    st.markdown(
        """
        **Fitness** — Your physiological metrics and race predictions.

        - **Aerobic decoupling** = Cardiac drift (HR vs pace) on long flat runs. Needs ≥6 sessions; single values are noise.
        - **VO2max** = Garmin Firstbeat daily estimate. ~5% error; underestimates at high levels (≥60 mL/kg/min).
        - **Age grading** = WMA 2023 age-graded % of open world record (outdoor runs ≥3 km).
        - **Thresholds** = Lactate threshold HR/pace from Garmin + critical velocity (fastest mile).
        - **LTHR anchors** = Best-effort 20/30-min LT estimates when Garmin LT is missing.
        - **Race predictions** = Garmin daily trend for 5K/10K/Half/Full. 5K/10K/Half most trustworthy.
        """
    )

    dec = windowed.get("load.decoupling")
    dec_mean = windowed.get("load.decoupling_mean")
    if (dec is None or dec.empty) and (dec_mean is None or dec_mean.empty):
        st.write("Aerobic decoupling needs ≥6 route-matched flat 90-min sessions with HR data.")
    else:
        fig = go.Figure()
        if dec is not None and len(dec):
            fig.add_trace(
                go.Scatter(
                    x=dec.index,
                    y=dec.values * 100,
                    mode="lines+markers",
                    name="Per-run decoupling",
                )
            )
        if dec_mean is not None and len(dec_mean):
            fig.add_trace(
                go.Scatter(
                    x=dec_mean.index,
                    y=dec_mean.values * 100,
                    mode="lines+markers",
                    name="Route-cluster mean",
                )
            )
        style(
            fig,
            title="Aerobic decoupling (HR vs pace drift)",
            hovermode="x unified",
            yaxis_title="%",
            xaxis_title="Date",
        )
        render(fig, width='stretch')
        if dec_mean is None or dec_mean.empty:
            n = len(dec) if dec is not None else 0
            st.caption(
                f"📊 **How to read:** Blue dots = individual run decoupling (HR/pace drift in 2nd half vs 1st half). "
                f"Positive % = cardiac drift (HR rising relative to pace). "
                f"{n}/6 route-matched sessions — **single-run values are noise**; the trend appears at ≥6. "
                + context_line(view, "load.decoupling")
            )
        else:
            st.caption(
                "📊 **How to read:** Orange line = mean decoupling across route-matched flat routes (≥90 min). "
                "Positive = cardiac drift (fatigue marker). Aggregated over ≥6 sessions for reliability. "
                + context_line(view, "load.decoupling_mean")
            )

    vo2 = windowed.get("fitness.vo2max")
    shape = windowed.get("vdot.shape")
    has_vo2 = vo2 is not None and not vo2.empty
    has_shape = shape is not None and not shape.empty
    if not has_vo2 and not has_shape:
        st.write(GARMIN_VO2MAX_HINT)
    else:
        fig = go.Figure()
        if has_vo2:
            fig.add_trace(
                go.Scatter(x=vo2.index, y=vo2.values, mode="lines+markers", name="VO2max")
            )
        if has_shape:
            # Runalyze shape rides the Garmin trend it compares against.
            fig.add_trace(
                go.Scatter(
                    x=shape.index,
                    y=shape.values,
                    mode="lines+markers",
                    name="Runalyze shape",
                    line={"color": PALETTE["orange"]},
                )
            )
        title = (
            "VO2max (Firstbeat estimate, daily trend)"
            if has_vo2
            else "Runalyze shape (no Garmin VO2max trend)"
        )
        style(fig, title=title, hovermode="x unified")
        render(fig, width='stretch')
        if has_shape:
            st.caption(
                "Runalyze-style shape: 30-day duration-weighted mean of per-run pace+HR VO2max, "
                "× race corrector (best-3). Independent of Garmin Firstbeat. "
                + context_line(view, "vdot.shape")
            )
        if has_vo2:
            st.caption(
                "📊 **How to read:** Daily Garmin Firstbeat VO2max estimate (mL/kg/min). "
                "**~5% error; underestimates ≥60 mL/kg/min.** Never recomputed here — taken directly from Garmin. "
                "Trend direction matters more than absolute value. "
                + context_line(view, "fitness.vo2max")
            )
        if not has_vo2:
            # Shape-only fallback: the Runalyze overlay is not a substitute for
            # the Garmin trend, so the refresh prompt still has to be visible.
            st.write(GARMIN_VO2MAX_HINT)

    ag = windowed.get("age_graded.pct")
    if ag is not None and len(ag):
        fig = go.Figure(
            go.Scatter(x=ag.index, y=ag.values * 100, mode="lines+markers", name="Age-graded %")
        )
        style(
            fig,
            title="Age-graded performance (WMA 2023)",
            hovermode="x unified",
            yaxis_title="% of open WR",
            xaxis_title="Date",
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Daily best WMA age-graded percentage over outdoor runs ≥3 km. "
            "100% = open-class world-record pace; ~80% ≈ national class, ~70% ≈ regional. "
            "WMA 2023 age factors; arbitrary distances log-log interpolated between event anchors. "
            + context_line(view, "age_graded.pct")
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
        style(
            fig,
            title=name,
            yaxis_title=("bpm" if params and params.get("unit") != "m/s" else f"min per {units}"),
            hovermode="x unified",
        )
        render(fig, width='stretch')
        # Add interpretation for each threshold chart
        if key == "load.lt_hr":
            st.caption(
                "📊 **How to read:** Garmin's lactate threshold HR estimate (bpm). ~7% error. "
                "Anchors your HR training zones. " + context_line(view, key)
            )
        elif key == "load.lt_pace":
            st.caption(
                "📊 **How to read:** Garmin's lactate threshold pace estimate. Can overestimate 20–26%. "
                "Use LTHR anchors below for best-effort pace validation. " + context_line(view, key)
            )
        elif key == "load.cs_approx":
            st.caption(
                "📊 **How to read:** Critical velocity approximation from your fastest mile. "
                "Represents sustainable pace for ~30–40 min. " + context_line(view, key)
            )

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
        style(
            fig,
            title=f"Garmin race predictions — {focus}",
            hovermode="x unified",
            yaxis_title="predicted time",
            yaxis={"tickvals": tickvals, "ticktext": ticktext},
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Daily Garmin race prediction trend from `/racepredictions/daily` "
            "(full ~365-day history, not just today's snapshot). "
            "**5K/10K/Half = trustworthy. Marathon = least trustworthy.** "
            "Step-line (HV) shows when predictions change. "
            "Hover for exact predicted time."
        )
    else:
        st.write(
            "Race predictions appear after a Refresh (requires new Garmin "
            "predictions in the payload)."
        )

    # Runalyze prognosis + marathon-shape detail (spec layout "B"): directly
    # under the Garmin race-predictions block above, and independent of it —
    # the prognosis table's own hide rule is the absent 5 km row.
    render_prognosis_block(windowed, view, units)


def render_prognosis_block(windowed, view, units) -> None:
    """Runalyze race prognosis table + marathon-shape detail.

    Columns are Distance | Raw | Damped (current-day values from the latest
    `prognosis.*` rows). Hidden entirely when the 5 km prognosis is absent, and
    the Damped column only appears where damped rows exist (see
    :func:`prognosis_rows`) — never a filler time.
    """
    rows = prognosis_rows(windowed)
    if not rows:
        return
    st.subheader("Runalyze race prognosis")
    st.dataframe(rows, width='stretch', hide_index=True)
    st.caption(
        "Runalyze-style prognosis ( Daniels-Gilbert, bisection-inverted; damped = marathon-shape-normalized)."
    )
    st.caption(
        "Races you tag in Garmin Connect (event type `Race`) anchor these — the tag must "
        "be present at the activity's first fetch here; later tags are not picked up."
    )

    pct_series = windowed.get("marathon.shape_pct")
    pct = last_value(pct_series) if pct_series is not None else None
    run_shape = windowed.get("vdot.shape")
    shape = last_value(run_shape) if run_shape is not None else None
    if pct is None or shape is None:
        return
    cols = st.columns(3)
    cols[0].metric("Marathon shape", f"{pct:.0f}%")
    cols[1].metric(
        "Target weekly volume", _fmt(marathon_shape.target_week_km(shape), {"unit": "km"}, units)
    )
    cols[2].metric(
        "Target long jog", _fmt(marathon_shape.target_lj_km_week_real(shape), {"unit": "km"}, units)
    )
    st.caption(
        "📊 **How to read:** Marathon shape = weekly mileage (67%) + long-jog quality (33%) "
        "against the targets for your current shape; 100% = marathon-ready. "
        + context_line(view, "marathon.shape_pct")
    )


def render_volume_tab(
    activities, view, windowed, units, selected_volume_agg="weekly", since=None, until=None
) -> None:
    is_monthly = selected_volume_agg == "monthly"
    grain = "Monthly" if is_monthly else "Weekly"

    # The buckets share one geometry, so one series identifies the partial ones.
    if since is not None and until is not None:
        freq = "MS" if is_monthly else "W-SUN"
        s = windowed.get("volume.distance_total_daily")
        partials = (
            partial_bucket_labels(s, since, until, freq) if s is not None and not s.empty else []
        )
        convention = (
            "Monthly buckets are calendar months, labelled by the 1st"
            if is_monthly
            else "Weekly buckets are Mon–Sun, labelled by their Monday"
        )
        st.caption(
            f"{convention}. A bucket that overlaps the selected window is shown in full, so the "
            "first and/or last bucket may include days outside the window. Partial buckets: "
            + ("; ".join(partials) if partials else "none")
            + "."
        )

    st.markdown(
        """
        **Volume & Terrain** — How much you've run and the terrain profile.

        - **Weekly distance** = Running + treadmill distance by week (cross-training has no distance, shows at 0).
        - **Weekly duration (hours)** = Time spent training across ALL sports (running + treadmill + cross + strength).
        - **Week-over-week changes** = % change in distance and duration week to week.
        - **Elevation gain** = Daily climbing (route context only, not a risk metric).
        - **Aggregation**: switch weekly/monthly via the sidebar toggle; weekly rolls up ISO weeks (Mon–Sun), monthly to calendar months.
        """
    )

    # Distance chart — per-sport daily series resampled to the selected grain
    weeks = {}
    for g in ("running", "treadmill", "cross"):
        key = f"volume.distance_{g}_daily"
        s = windowed.get(key)
        if s is not None and len(s):
            weeks[g] = s
    if weeks:
        fig = go.Figure()
        for g, s in weeks.items():
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=g))
        total_dist = windowed.get("volume.distance_total_daily")
        roll = None
        if total_dist is not None and len(total_dist):
            roll = volume.rolling_4wk(total_dist)
        if roll is not None and len(roll):
            fig.add_trace(
                go.Scatter(
                    x=roll.index,
                    y=roll.values,
                    name="rolling 4wk",
                    yaxis="y2",
                    line={"color": PALETTE["orange"]},
                )
            )
            style(
                fig,
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"rolling {'mi' if units in ('miles', 'imperial') else 'km'}",
                },
            )
        style(fig, title=f"{grain} distance", barmode="stack", hovermode="x unified")
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Stacked bars = weekly distance by sport. Blue = outdoor running, "
            "orange = treadmill, green = cross-training (no distance, stacks at 0). "
            "Orange line = 4-week rolling average (smoothed trend). "
            + context_line(view, "volume.distance_total")
        )
    else:
        st.write("No volume data in this window.")

    # Time-volume chart (hours/week) — includes cross-training, which has no
    # distance but still consumes time/energy.
    hours = {
        g: windowed.get(f"volume.duration_{g}_daily")
        for g in ("running", "treadmill", "cross", "strength")
    }
    hours = {g: s for g, s in hours.items() if s is not None and len(s)}
    if hours:
        fig = go.Figure()
        for g, s in hours.items():
            fig.add_trace(go.Bar(x=s.index, y=s.values, name=g))
        total_hrs = windowed.get("volume.duration_total_daily")
        roll = volume.rolling_4wk(total_hrs) if total_hrs is not None else None
        if roll is not None and len(roll):
            fig.add_trace(
                go.Scatter(
                    x=roll.index,
                    y=roll.values,
                    name="rolling 4wk",
                    yaxis="y2",
                    line={"color": PALETTE["orange"]},
                )
            )
        style(fig, yaxis2={"overlaying": "y", "side": "right", "title": "rolling hours"})
        style(
            fig,
            title=f"{grain} duration (hours)",
            barmode="stack",
            hovermode="x unified",
            yaxis_title="hours",
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Stacked bars = weekly training TIME (hours) by sport. "
            "Includes cross-training and strength (which have no distance but consume energy). "
            "Orange line = 4-week rolling average. Compare with distance chart above to see "
            "if you're getting similar time from non-running activities. "
            + context_line(view, "volume.duration_total")
        )

        wow = volume.week_over_week_pct(windowed.get("volume.duration_total_daily"))
        if wow is not None and len(wow):
            fig = go.Figure(go.Bar(x=wow.index, y=wow.values * 100, name="week-over-week %"))
            fig.add_hline(y=0, line_dash="dot", line_color=PALETTE["gray"])
            style(
                fig,
                title=f"{'Month-over-month' if is_monthly else 'Week-over-week'} duration change",
                hovermode="x unified",
                yaxis_title="%",
            )
            render(fig, width='stretch')
            st.caption(
                "📊 **How to read:** % change in total weekly training hours vs previous week. "
                "Positive = more time training; negative = less. Large spikes (>30%) warrant attention."
            )

    wow = volume.week_over_week_pct(windowed.get("volume.distance_total_daily"))
    if wow is not None and len(wow):
        fig = go.Figure(go.Bar(x=wow.index, y=wow.values * 100, name="week-over-week %"))
        fig.add_hline(y=0, line_dash="dot", line_color=PALETTE["gray"])
        style(
            fig,
            title=f"{'Month-over-month' if is_monthly else 'Week-over-week'} distance change",
            hovermode="x unified",
            yaxis_title="%",
        )
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** % change in weekly running+treadmill distance vs previous week. "
            "Positive = more distance; negative = less. The '10% rule' is a rough guideline, not a law."
        )

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
                    x=roll28.index,
                    y=roll28.values,
                    name="rolling 28d",
                    line={"color": PALETTE["orange"]},
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
                    line={"color": PALETTE["blue"], "dash": "dash"},
                )
            )
            style(
                fig,
                yaxis2={
                    "overlaying": "y",
                    "side": "right",
                    "title": f"{'ft' if units in ('miles', 'imperial') else 'm'} / {'mi' if units in ('miles', 'imperial') else 'km'}",
                },
            )
        style(fig, title="Elevation gain (running)", hovermode="x unified")
        render(fig, width='stretch')
        st.caption(
            "📊 **How to read:** Blue bars = daily elevation gain (route profile). "
            "Orange line = 28-day rolling average (climbing trend). "
            "Dashed blue = gain per km/mi (hilliness normalized by distance). "
            "**Elevation is route context, not a risk metric.**"
        )
    else:
        st.write("No elevation data in this window.")


def _fetch_details_for_reps(reps: list, cache_dir: Path) -> dict[int, tuple]:
    """Group-scoped, cache-first details fetch for the repetition comparison.

    Like _fetch_series_for, but scoped to one workout group: disk cache first
    (``activity_details_{id}.json``), network only for ids with no cached
    file, per-call ``run_with_timeout``, and a broad per-rep skip so one dead
    fetch (timeout, auth failure, malformed payload) never aborts the rest of
    the group. The gateway is constructed only when at least one id actually
    needs the network. Never raises — missing/failed reps are simply absent
    from the result and callers degrade them to None.
    """
    out: dict[int, tuple] = {}
    pending = []
    for a in reps:
        path = cache_dir / f"activity_details_{a.activity_id}.json"
        if not path.exists():
            pending.append(a)
            continue
        try:
            parsed = parse_details_series(json.loads(path.read_text()))
            if parsed[0]:
                out[a.activity_id] = parsed
        except Exception:  # noqa: BLE001 - unreadable cache → skip (matches _fetch_series_for)
            pass
    if not pending:
        return out
    try:
        gw = GarminGateway(cache_dir=cache_dir)
    except Exception:  # noqa: BLE001 - auth unavailable → fastest-30s degrades to None
        return out
    for a in pending:
        try:
            details = run_with_timeout(
                gw.fetch_activity_details, timeout=DETAILS_TIMEOUT_S, activity_id=a.activity_id
            )
            parsed = parse_details_series(details)
            if parsed[0]:
                out[a.activity_id] = parsed
        except Exception:  # noqa: BLE001 - one dead rep must not abort the group
            pass
    return out


def render_activity_laps(activities, activity_id: int, units: str) -> None:
    """Garmin laps/splits + running dynamics for the selected activity.

    Splits are fetched on demand for this one activity only, cache-first
    (``activity_splits_{id}.json`` on disk beats the network — same rule as
    the repetition-comparison details). Everything degrades: no auth, 404,
    timeout or a malformed payload becomes a caption — one dead fetch never
    breaks the dashboard.
    """
    if next((a for a in activities if a.activity_id == activity_id), None) is None:
        return
    payload = None
    cache_path = APP_CACHE_DIR / f"activity_splits_{activity_id}.json"
    if cache_path.exists():
        payload = _load_json_file(cache_path)  # unreadable cache → refetch
    if payload is None:
        try:
            gw = GarminGateway(cache_dir=APP_CACHE_DIR)
            payload = run_with_timeout(
                gw.fetch_activity_splits, timeout=DETAILS_TIMEOUT_S, activity_id=activity_id
            )
        except Exception:  # noqa: BLE001 - laps are a bonus panel, never fatal
            st.caption("No lap data available for this activity (Garmin fetch failed).")
            return
    laps = parse_laps(payload if isinstance(payload, dict) else {})
    if not laps:
        st.caption("No laps/splits recorded for this activity.")
        return
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    rows = []
    for lap in laps:
        pace = (
            m_per_unit * lap.duration_s / lap.distance_m
            if lap.distance_m and lap.duration_s
            else None
        )
        rows.append(
            {
                "lap": lap.index,
                "distance": _fmt(lap.distance_m / 1000.0, {"unit": "km"}, units)
                if lap.distance_m is not None
                else "—",
                "time": _fmt(lap.duration_s, {"unit": "s"}, units)
                if lap.duration_s is not None
                else "—",
                "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
                "avg HR": f"{lap.avg_hr:.0f}" if lap.avg_hr is not None else "—",
                "cadence": f"{lap.avg_cadence:.0f}" if lap.avg_cadence is not None else "—",
                "elev gain": _fmt(lap.elevation_gain_m, {"unit": "m"}, units)
                if lap.elevation_gain_m is not None
                else "—",
            }
        )
    st.subheader("Garmin laps / splits")
    st.dataframe(rows, width='stretch', hide_index=True)

    def _lap_mean(values) -> float | None:
        present = [v for v in values if v is not None]
        return sum(present) / len(present) if present else None

    dynamics = {
        "avg cadence": _lap_mean(lap.avg_cadence for lap in laps),
        "stride length": _lap_mean(lap.stride_length_m for lap in laps),
        "ground contact": _lap_mean(lap.ground_contact_time for lap in laps),
        "vertical oscillation": _lap_mean(lap.vertical_oscillation_cm for lap in laps),
    }
    parts = []
    cadence = dynamics["avg cadence"]
    if cadence is not None:
        parts.append(f"avg cadence {cadence:.0f} spm")
    stride = dynamics["stride length"]
    if stride is not None:
        parts.append(f"stride {stride:.2f} m")
    gct = dynamics["ground contact"]
    if gct is not None:
        parts.append(f"GCT {gct:.0f} ms")
    vo = dynamics["vertical oscillation"]
    if vo is not None:
        parts.append(f"vert osc {vo:.1f} cm")
    st.caption(
        "Running dynamics: " + (" · ".join(parts) if parts else "— not recorded on these laps")
    )


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
    # Fastest-30s, computed on demand for THIS comparison group only: details
    # are ensured cache-first for the group's pace-capable reps (running +
    # treadmill — treadmill pace is real; missing speed falls out via
    # best_window → None). Never persisted, never in the refresh path.
    # Strength/cross reps have no meaningful pace and missing/failed details
    # stay None.
    candidates = [a for a in reps if a.sport in ("running", "treadmill")]
    series = _fetch_details_for_reps(candidates, APP_CACHE_DIR)
    fast30 = {a.activity_id: fastest_30s_pace(series, a.activity_id) for a in candidates}
    table = []
    for a in reps:
        m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
        pace = m_per_unit / a.avg_speed if a.avg_speed else None
        gap_pace = None
        if a.sport == "running" and a.avg_speed and a.ele_gain_m is not None and a.distance_m > 0:
            grade = a.ele_gain_m / a.distance_m
            gap_speed = a.avg_speed * (1.0 + GAP_K * grade)
            gap_pace = m_per_unit / gap_speed if gap_speed > 0 else None
        table.append(
            {
                "date": a.date.isoformat(),
                "distance": _fmt(a.distance_km, {"unit": "km"}, units),
                "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
                "GAP": _fmt(gap_pace, {"unit": "s"}, units) if gap_pace else "—",
                "avg HR": f"{a.avg_hr:.0f}" if a.avg_hr is not None else "—",
                "max HR": f"{a.max_hr:.0f}" if a.max_hr is not None else "—",
                "aerob. TE": f"{a.aerobic_te:.1f}" if a.aerobic_te is not None else "—",
                "anaerob. TE": f"{a.anaerobic_te:.1f}" if a.anaerobic_te is not None else "—",
                "elev gain": _fmt(a.ele_gain_m, {"unit": "m"}, units)
                if a.ele_gain_m is not None
                else "—",
                **_rpe_wx_cols(a, units),
                "fast30s": (
                    _fmt_pace_min((m_per_unit / fast30[a.activity_id]) / 60.0, units)
                    if fast30.get(a.activity_id)
                    else "—"
                ),
            }
        )
    st.subheader(f"Repeated workout **{code}**")
    st.caption(
        "📊 **How to read:** Table shows every repetition of this workout (same 80/20 code), oldest first. "
        "GAP = Grade-Adjusted Pace (compensates for elevation). TE = Garmin Training Effect. "
        "RPE/Feel = watch-side rating (— until backfilled); weather columns are Garmin-recorded "
        "conditions; Fast30s = fastest 30 s pace of the rep. "
        "Compare pace/HR/GAP across reps to track fitness progression on the same workout."
    )
    st.dataframe(
        table,
        width='stretch',
        hide_index=True,
    )

    xs = [a.date for a in reps]
    m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
    paces = [m_per_unit / a.avg_speed / 60.0 if a.avg_speed else None for a in reps]
    gap_paces = []
    for a in reps:
        if a.sport == "running" and a.avg_speed and a.ele_gain_m is not None and a.distance_m > 0:
            grade = a.ele_gain_m / a.distance_m
            gap_speed = a.avg_speed * (1.0 + GAP_K * grade)
            gap_paces.append(m_per_unit / gap_speed / 60.0 if gap_speed > 0 else None)
        else:
            gap_paces.append(None)
    fast30_paces = [
        m_per_unit / fast30[a.activity_id] / 60.0 if fast30.get(a.activity_id) else None
        for a in reps
    ]
    hrs = [a.avg_hr for a in reps]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=paces, mode="lines+markers", name="avg pace (min/unit)"))
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=gap_paces,
            mode="lines+markers",
            name="GAP (min/unit)",
            line={"color": PALETTE["magenta"], "dash": "dot"},
        ),
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=fast30_paces,
            mode="lines+markers",
            name="Fast30s (min/unit)",
            line={"color": PALETTE["blue"], "dash": "dash"},
        ),
    )
    fig.add_trace(
        go.Scatter(x=xs, y=hrs, mode="lines+markers", name="avg HR (bpm)", yaxis="y2"),
    )
    style(
        fig,
        title=f"Performance over repetitions — {code}",
        hovermode="x unified",
        yaxis_title=f"min / {'mi' if units not in ('km', 'metric') else 'km'}",
        yaxis2={"overlaying": "y", "side": "right", "title": "bpm"},
    )
    render(fig, width='stretch')
    st.caption(
        "📊 **How to read:** Blue = average pace. Purple dotted = GAP (pace adjusted for hills — "
        "compares effort across hilly/flat routes). Teal dashed = Fast30s (fastest 30 s pace of "
        "each rep; gaps mean no pace data). Average HR = right axis. "
        "Improving fitness = pace/GAP gets faster at same or lower HR."
    )


def render_activities(activities, since, until, units, profile) -> None:
    st.markdown(
        """
        **Activities** — Your raw activity log. Click a row to compare repetitions of the same 80/20 workout.

        - **GAP** = Grade-Adjusted Pace (compensates for elevation).
        - **TE** = Garmin Training Effect (aerobic/anaerobic).
        - **VO2max** = Per-run Firstbeat estimate.
        - **Graded** = WMA age-graded % of open world record (outdoor runs ≥3 km).
        - **RPE/Feel** = watch-side session rating; **Temp/Hum/Wind/Wx** =
          Garmin-recorded conditions (°C/km/h when Units = km, °F/mph when miles).
        """
    )
    bm_period = best_mile([a for a in activities if since <= a.date <= until])
    bm_all = best_mile(activities)
    cols = st.columns(2)
    cols[0].metric(
        "Best mile (period)",
        _fmt(bm_period["value_s"], {"unit": "s"}, units) if bm_period else "—",
    )
    cols[1].metric(
        "Best mile (all-time)",
        _fmt(bm_all["value_s"], {"unit": "s"}, units) if bm_all else "—",
    )
    rows = []
    for a in activities:
        if not (since <= a.date <= until):
            continue
        m_per_unit = 1000.0 if units in ("km", "metric") else 1609.344
        pace = m_per_unit / a.avg_speed if a.avg_speed else None
        gap_pace = None
        if a.sport == "running" and a.avg_speed and a.ele_gain_m is not None and a.distance_m > 0:
            grade = a.ele_gain_m / a.distance_m
            gap_speed = a.avg_speed * (1.0 + GAP_K * grade)
            gap_pace = m_per_unit / gap_speed if gap_speed > 0 else None
        graded = activity_graded_pct(a, profile)
        rows.append(
            {
                "date": a.date.isoformat(),
                "sport": a.sport,
                "name": a.name or "—",
                "distance": _fmt(a.distance_km, {"unit": "km"}, units),
                "duration": _fmt(a.duration_s, {"unit": "s"}, units),
                "pace": _fmt(pace, {"unit": "s"}, units) if pace else "—",
                "GAP": _fmt(gap_pace, {"unit": "s"}, units) if gap_pace else "—",
                "graded": f"{graded * 100:.1f}%" if graded is not None else "—",
                "avg HR": f"{a.avg_hr:.0f}" if a.avg_hr is not None else "—",
                "max HR": f"{a.max_hr:.0f}" if a.max_hr is not None else "—",
                "cadence": f"{a.avg_cadence:.0f}" if a.avg_cadence is not None else "—",
                "VO2max": f"{a.vo2max:.1f}" if a.vo2max is not None else "—",
                "elev gain": _fmt(a.ele_gain_m, {"unit": "m"}, units)
                if a.ele_gain_m is not None
                else "—",
                # RPE/Feel/weather only — no Fast30s here: computing it for
                # every row would need per-row details fetches (the precompute
                # the owner rejected); it lives in the comparison table only.
                **_rpe_wx_cols(a, units),
                "activity_id": a.activity_id,
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        st.write("No activities in this window.")
        return
    df = df.sort_values("date", ascending=False).reset_index(drop=True)
    st.caption(
        "📊 **How to read:** Click any row to see all repetitions of that workout (same 80/20 code) "
        "and track your progression over time."
    )
    sel = st.dataframe(
        df.drop(columns=["activity_id"]),
        width='stretch',
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="activities_table",
    )
    if sel.selection.rows:
        selected_id = int(df.iloc[sel.selection.rows[0]]["activity_id"])
        render_activity_laps(activities, selected_id, units)
        render_repetitions(activities, selected_id, units)


def render_paces_table(windowed, units) -> None:
    """Runalyze training paces from the current shape — the Fitness tab's last block.

    Derived in-session, never stored (spec layout "B"). Hidden when the window
    has no `vdot.shape` rows: there is no current shape to read paces from.
    """
    run_shape = windowed.get("vdot.shape")
    shape = last_value(run_shape) if run_shape is not None else None
    rows = pace_table_rows(shape, units)
    if not rows:
        return
    st.subheader("Training paces (Runalyze vVO2max)")
    st.dataframe(rows, width='stretch', hide_index=True)
    st.caption(
        "📊 **How to read:** Pace at 65–110% of vVO2max (100% ≈ the maximal pace for ~11 min) "
        f"from your current shape, rounded to {daniels_gilbert.round_half_up(shape)}. "
        "Display-only — never stored."
    )


def main() -> None:
    st.title("Training Tracker")
    try:
        store = get_store()
    except Exception as exc:
        # An unopenable store (a newer schema, a data/ that cannot be created)
        # used to surface as a bare traceback with no way forward.
        st.error(f"Cannot open the training store at {DB_PATH}: {exc}")
        st.caption(
            "Restore a backup from `data/backups/` (see the README), "
            "or run `refresh.py` to rebuild the store."
        )
        st.stop()
        return

    persisted_profile = store.load_runner_profile()
    _units_display = "km" if (persisted_profile or default_profile()).units == "metric" else "miles"
    units = st.sidebar.radio("Units", ("km", "miles"), index=0 if _units_display == "km" else 1)

    if "activities" not in st.session_state:
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
    # Fingerprint-guarded reload: session state survives reruns (including
    # file-change auto-reruns after a migration), so a plain once-only load
    # would feed stale Activity objects to the pipeline indefinitely.
    resolve_activities(store, st.session_state)
    # Remember the health log's timestamp before the poll runs, so the poll can
    # tell a new verdict from the one this session already showed.
    st.session_state["_health_mtime"] = _health_log_mtime()
    # After resolve_activities, so the fingerprint it just stored is the one
    # the poll compares against (no spurious rerun on the first tick).
    poll_store_changes()

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
                st.session_state["_period_needs_reset"] = True
            # Reload after every successful refresh: main's save above AND the
            # catch-up pass inside refresh_garmin both write rows, and a
            # catch-up-only rewrite doesn't move the activities fingerprint
            # (row count / elapsed total unchanged), so resolve_activities
            # alone would keep serving stale objects.
            st.session_state["activities"] = store.load_activities()
            # Trend payloads update even when there are no new activities.
            st.session_state["lt_payload"] = lt
            st.session_state["race_payload"] = race
            st.session_state["vo2max_payload"] = vo2
            # Mark that a Garmin refresh happened so details get re-fetched
            # with the current period window below.
            st.session_state["_garmin_refreshed"] = True
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

    # The scheduled health check writes to logs/health.log; nobody reads that
    # log, so surface the last outcome here — a failed check means the nightly
    # refresh may have died silently (expired Garmin token, locked store).
    _health = health_status()
    if _health is not None:
        _status, _when = _health
        if _status == "FAIL":
            st.sidebar.warning(f"Health check FAILED ({_when})")
        else:
            st.sidebar.caption(f"Health check: {_status} ({_when})")

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
    volume_agg = st.sidebar.radio(
        "Volume aggregation",
        ("weekly", "monthly"),
        index=0 if profile.selected_volume_agg != "monthly" else 1,
        horizontal=True,
        key="volume_agg_widget",
    )
    _persist_selected_fields(store, profile.selected_race, volume_agg)
    st.session_state["selected_kpi_labels"] = selected_kpi_labels

    min_d, default_since, max_d = period_bounds([a.date for a in activities])

    # After a refresh added activities, the period must cover the newest ones.
    # Streamlit cannot reset a keyed widget with a script-body `pop`: the value
    # updates Python-side for one run only, while the browser keeps the stale
    # range and re-adopts it on the next rerun (streamlit #5442) — hiding newly
    # fetched activities from the Activities tab. A NEW widget key forces the
    # browser to adopt the reset value; the user's start date is preserved and
    # the end extends to the new latest activity date.
    if st.session_state.pop("_period_needs_reset", False):
        _ver = st.session_state.get("_period_key_version", 0)
        old_period = st.session_state.get(f"{PERIOD_KEY}_{_ver}")
        st.session_state["_period_reset_value"] = reset_period_after_refresh(
            old_period, default_since, max_d
        )
        st.session_state["_period_key_version"] = _ver + 1

    _period_key = f"{PERIOD_KEY}_{st.session_state.get('_period_key_version', 0)}"
    period = st.sidebar.date_input(
        "Period",
        value=st.session_state.pop("_period_reset_value", None) or (default_since, max_d),
        min_value=min_d,
        max_value=max_d,
        key=_period_key,
    )
    st.sidebar.caption(f"Store range {min_d} → {max_d} · {len(activities)} activities")
    if isinstance(period, (list, tuple)) and len(period) == 2:
        period_since, period_until = period
    else:
        period_since = period_until = period

    # Ensure date objects for type safety (date_input may return datetime)
    since = period_since.date() if hasattr(period_since, "date") else period_since
    until = period_until.date() if hasattr(period_until, "date") else period_until

    # Re-fetch activity details when the period changes or after a Garmin
    # refresh.  The top-k is scoped to [since, until] so dots reflect the
    # fastest qualifying efforts *within the visible window*.
    _period_key = (since, until)
    _need_details = (
        st.session_state.pop("_garmin_refreshed", False)
        or st.session_state.get("_details_period_key") != _period_key
    )
    if _need_details and activities:
        try:
            gw = GarminGateway(cache_dir=APP_CACHE_DIR)
            lthr_series = run_with_timeout(
                fetch_details_for_lthr,
                timeout=DETAILS_TOTAL_TIMEOUT_S,
                gw=gw,
                activities=activities,
                since=since,
                until=until,
            )
            dec_series = run_with_timeout(
                fetch_details_for_decoupling,
                timeout=DECOUPLING_TOTAL_TIMEOUT_S,
                gw=gw,
                activities=activities,
                since=since,
                until=until,
            )
        except Exception as exc:
            # No Garmin auth (or a failed detail fetch) must not take the app
            # down — the stored data still renders without the detail series.
            st.sidebar.warning(f"Garmin detail fetch failed: {exc.__class__.__name__}: {exc}")
        else:
            st.session_state["series_by_id"] = {**lthr_series, **dec_series}
            st.session_state["_details_period_key"] = _period_key

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
    windowed = view.windowed(since, until, volume_agg)

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
        # Runalyze training paces close the Fitness tab (spec layout "B").
        render_paces_table(windowed, units)
    with tab_volume:
        render_volume_tab(activities, view, windowed, units, volume_agg, since=since, until=until)
    with tab_acts:
        render_activities(activities, since, until, units, profile)

    with st.expander("Technical Reference", expanded=False):
        st.markdown("""
        **Detailed technical notes, formulas, and limitations.** The charts above have inline "How to read" captions — this section is for deep dives.
        """)
        for line in GLOSSARY:
            st.markdown(f"- {line}")


if __name__ == "__main__":
    main()
