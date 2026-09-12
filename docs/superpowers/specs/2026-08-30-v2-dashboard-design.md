# v2 Training Dashboard — Design

## 1. Goal

An interactive Streamlit + plotly dashboard that fronts the complete v2
measurement layer. It shows every emitted metric as a graph with
interpretation context, lets the user customize the displayed date range and
display units, and lets the user set key profile inputs (HRmax, HRrest, sex,
birth year, optional LTHR) that immediately recompute the HR-derived series.

v1 (`v1/app.py`) was abandoned: its problems were data/metrics plumbing, not
the framework. We do not reuse or migrate it; the new UI reads only v2 data
paths.

## 2. Scope decisions (from brainstorming)

- **Framework:** Streamlit + plotly.
- **Data source:** live Garmin fetch in-app via `GarminGateway`
  (`v2/gateway.py`); results are appended/merged into a **persistent SQLite
  DB** so history accumulates across sessions.
- **Refresh trigger:** manual "Refresh from Garmin" button only. No
  auto-fetch on launch, no freshness guard.
- **Architecture:** Approach 1 — an isolated, pure, testable session-view
  module (`session.py`) plus a thin Streamlit view (`dashboard.py`). The
  persistent DB stores activities + profile (source of truth); the dashboard
  recomputes displayed series from those in memory. Computed `metric_series`
  in the DB are not used by the dashboard (avoids stale-series bugs on
  profile edits).

## 3. Architecture & data flow

### 3.1 Components

- `v2/session.py` (new, pure — no Streamlit, no network):
  - `compute_metric_rows(activities, profile, lt_payload, race_payload) -> list[dict]`
  - `build_session_view(...)` returns the metric rows organized as
    `dict[str, pd.Series or pd.DataFrame]` plus a context object carrying the
    resolved profile and per-metric `params`/`flags` for interpretation
    badges.
  - Week-label date filtering (volume rows included by week end — the same
    rule as `e2e_report.in_period`) and unit formatting moved here so both the
    CLI report and the dashboard share one implementation.
- `v2/dashboard.py` (new, Streamlit):
  - Sidebar: Garmin refresh, profile editor, date-range, units.
  - Main area: KPI cards + tabbed charts rendered with plotly.
  - Thin by design: all data shaping lives in `session.py`.
- `v2/pipeline.py` (refactor): extract the row-building body of
  `run_pipeline` into `compute_metric_rows(...)`; `run_pipeline` becomes a
  shim that writes those rows through `MetricStore`. Behavior identical —
  existing tests + `e2e_report` must stay green. Guarantees the dashboard can
  never drift from the CLI report's computation.
- `v2/store.py` (extend): add `load_activities()` (DB rows → `Activity`) and
  `load_runner_profile()` (→ `RunnerProfile`). Today only savers exist.

### 3.2 Data flow

1. Launch: open persistent DB, load activities + profile.
2. **Refresh from Garmin** (button): `GarminGateway.fetch_activities(...)` +
  `fetch_lactate_threshold()` + `fetch_race_predictions()` → normalize via
  `from_summary` → merge into DB (upsert by `activity_id`) → recompute session
  view.
3. Profile edit (e.g. HRmax change): recompute session view locally; never
  hits Garmin.
4. Date-range / units change: filter and reformat already-computed series;
  no recompute.
5. Session view cached with `st.cache_data` keyed on a fingerprint of
  (activities, profile, payloads) so widget fiddling (date-range, unit toggles)
  does not re-run the pipeline.

### 3.3 Activity round-trip note

The `activities` table does **not** persist `elapsed_s`, `ele_loss_m`,
`avg_cadence`, or `ts_ms`. No emitted metric depends on them, so reloading
activities from the DB reproduces identical metric values. This is documented
here; if a future metric needs those fields, the table must be extended (out
of scope).

## 4. Persistent DB & profile

- DB path: `v2/data/training.sqlite`, gitignored via `v2/.gitignore`;
  overridable with env var `TRAINING_DB`.
- Reuses existing `MetricStore` schema. `activities` and `runner_profile` are
  the source of truth. `metric_series` remains for CLI/tooling; the dashboard
  does not read stale series from it.
- **Profile editor** (sidebar):
  - HRmax source — radio: *manual* (numeric field), *estimate from workouts*
    (recurring observed max via `with_estimated_hrmax`), or *age-predicted*.
  - HRrest (bpm), sex (M/F), birth year, optional LTHR override (bpm),
    units (km/miles).
  - Changes persist to the DB and immediately recompute load/PMC/ACWR.

## 5. Charts & interpretation context

Tabbed layout with a KPI row of current in-window values on top: ACWR,
    CTL, ATL, TSB, weekly distance, VO2max, LT HR.

- **Tab 1 — Load & Recovery**
  - Daily TRIMP bars (`load.banister`, toggle to Edwards) — caption shows
    Banister params + `hrmax_source`; note "TRIMP is proportional to the chosen
    HRmax" (spec §7.2).
  - PMC lines (`pmc.ctl` / `pmc.atl` / `pmc.tsb`, TSB on secondary axis).
  - ACWR line with 0.8–1.3 band shading; note "ACWR banded as injury prediction" (spec §6).
  - Cross-training TRIMP (`load.banister_cross` / `load.edwards_cross`) when
    present, `basis=cross_training` flagged.
- **Tab 2 — Fitness**
  - `fitness.vo2max` trend (Firstbeat error note, spec §7.4).
  - `load.lt_hr` / `load.lt_pace` / `load.cs_approx` over time (LT-pace
    anchor caveat, spec §7.5).
  - Race predictions `race_5k/10k/half/full` as step-lines per distance;
    "marathon least trustworthy" note (spec §7.6).
- **Tab 3 — Volume & Terrain**
  - Weekly `volume.distance_total/running/treadmill/cross` grouped bars +
    rolling4wk line + week-over-week %.
  - `elevation.daily/rolling28d/gain_per_km` context (route context, not a
    risk metric).
- **Tab 4 — Activities**
  - Recent-workout table (date, sport, distance, duration, pace, avg/max HR,
    VO2max, elevation) filtered to the selected range, newest first.

**Interpretation surfacing** (the spec's "trend over absolute, error bars,
context" philosophy):
- Each caption renders per-metric `params`/`flags` as small badges (e.g.
  `hrmax=185 (configured)`, `error_class=firstbeat_estimate_5pct`,
  `basis=cross_training`).
- A "How to read this" glossary expander carries the spec's interpretation
  rules and the §7 limitation list.
- Missing-but-expected data shows an explanatory note instead of a blank
  chart, mirroring `e2e_report`'s conditional-metric messaging (e.g. ACWR
  needs ≥28d running span; cross TRIMP needs HR data).

## 6. Controls & layout

- **Date range:** sidebar date pair over the data min..max; default trailing
  180 days (covers ACWR 28d + history-percentile 180d + PMC warmup context).
  Week-labeled rows filter by week end.
- **Units:** km/miles toggle (profile editor); distances/pace/elevation
  reformat.
- **Layout:** `wide` page config. Sidebar = refresh + profile editor + date
  range + units. Main area = KPI cards then tabs.
- **First-run onboarding:** empty DB before first refresh → instructions
  ("Connect Garmin", what to expect), not a crash.

## 7. Error handling

- Garmin auth/fetch failure on refresh → `st.warning`, dashboard keeps
  serving the DB unchanged. Never crash, never silently empty-stale.
- Selected range with no rows → explicit "no data in this window".
- Empty DB with no refresh yet → onboarding message.
- Live fetch returns no new activities → "already up to date" info.

## 8. Testing

- `v2/tests/test_session.py`: fixture activities + profiles — expected metric
  keys present, dates sorted, `params`/`flags`/profile context attached,
  week-row date filtering, unit formatting (km/miles).
- `v2/tests/test_store.py` additions: `load_activities` / `load_runner_profile`
  round-trips.
- Existing pipeline + `e2e_report` tests must stay green after the
  `compute_metric_rows` extraction.
- Dashboard UI itself is thin and is not unit-tested (Streamlit rendering is
  out of scope).

## 9. Files touched

New: `v2/session.py`, `v2/dashboard.py`, `v2/tests/test_session.py`.
Modified: `v2/pipeline.py`, `v2/store.py`, `v2/pyproject.toml`, `v2/.gitignore`,
`v2/tests/test_store.py`.