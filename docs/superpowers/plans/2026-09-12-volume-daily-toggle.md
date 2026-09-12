# Volume Daily-Source + Monthly Toggle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` or `superpowers:subagent-driven-development` to implement this plan task-by-task.

**Goal:** Replace the weekly/monthly rollup pane with a single Weekly/Monthly toggle that resamples the existing volume charts (distance bar, duration bar, both WoW-% charts) from a daily-source-of-truth, persist the user's selection across dashboard loads (mirroring the race-predictor `selected_race` pattern), and remove the now-redundant rollup pane.

**Architecture:** Volume metrics change from pre-aggregated-at-ingest weekly series to a **daily source series** stored in the DB; `SessionView.windowed()` resamples that daily series to either ISO-week or calendar month on demand based on the profile's persisted `selected_volume_agg`. This is the single-source-of-truth design: weekly and monthly can never drift because both derive from the same daily grain. The PMC/CTL/ATL/TSB charts are untouched (they are already daily `load.*` series). The rollup pane (`session.rollup` + its dashboard renderer) is deleted entirely.

**Tech Stack:** Python 3.12, pandas, Streamlit, SQLite (`store.py` / `MetricStore`). TDD (failing test → minimal code). ruff (line-length 100, `E501` ignored). mypy is configured in AGENTS.md but **not installed in the venv** — do not block on it.

**Spec:** `docs/reviews/2026-09-12-expansion.md` Addendum (personal-only reversal; the brief's age-grading criterion is satisfied by the prior work `4fdd2e1`). The scope for *this* plan: "the new weekly-monthly rollup pane adds nothing... remove the pane. Instead, add a monthly toggle to the existing [volume] charts. As with other toggles (eg race predictor), maintain the most recent selection across loads."

## Global Constraints
- No new `Activity` fields needed — uses existing `distance_m`, `duration_s`, `sport`, `date`, `fastest_split_1609`.
- Persistence model mirrors race distance: `RunnerProfile.selected_volume_agg: str = "weekly"` stored in the `runner_profile` SQLite table via `store.save_runner_profile`, read from persisted profile on load.
- No DB schema migration of the `metric_series` table schema itself — `metric` is a free-text key, so daily/weekly/monthly rows coexist. Legacy weekly `volume.*` rows are pruned on migration via the existing `store.prune_legacy_keys`.
- Volume rules (unchanged from current `metrics/volume.py`): distance counts running+treadmill only; duration counts all sports including cross+strength. Daily series is per-group (`distance_{group}_daily`, `duration_{group}_daily`).
- The daily volume series carries `params={"agg": "daily", "unit": "km"/"h"}` so session can identify and resample it.
- ISO-week resampling in session must produce identical values to the old pipeline weekly aggregation (so existing tests/values don't wobble). Calendar-month = `resample("ME").sum()`.

---

## Task 1: Daily volume source series in `metrics/volume.py`

**Files:**
- Create: (none — extend existing)
- Modify: `metrics/volume.py`
- Test: `tests/test_volume.py`

**Interfaces:**
- Consumes: `Activity` from `normalize`
- Produces: `daily_distance(activities, group) -> pd.Series` (daily index, km; running+treadmill only), `daily_duration_hours(activities, group) -> pd.Series` (daily index, hours; all sports), `resample_agg(series, freq) -> pd.Series` (resample to W or ME), `rolling_4wk(...)`, `week_over_week_pct(...)` retained for parity but operate on the resampled output.
- Also delete: `weekly_distance`, `weekly_duration_hours` (replaced by daily + resample).

- [ ] **Step 1: Write failing tests for daily series**
```python
# tests/test_volume.py additions
def test_daily_distance_running_only():
    acts = [_act(date(2026,4,6),"running",10_000.0), _act(date(2026,4,7),"cross",0.0)]
    d = daily_distance(acts, "running")
    assert d["2026-04-06"] == pytest.approx(10.0)
    assert "2026-04-07" not in d  # cross excluded from distance

def test_daily_duration_includes_cross():
    acts = [_act(date(2026,4,6),"running",10_000.0,3600), _act(date(2026,4,7),"cross",0.0,1800)]
    d = daily_duration_hours(acts, "total")
    assert d["2026-04-06"] == pytest.approx(1.0)
    assert d["2026-04-07"] == pytest.approx(0.5)
```

- [ ] **Step 2: Run tests — expect FAIL**
- [ ] **Step 3: Implement `daily_distance` / `daily_duration_hours`**
```python
# metrics/volume.py
def daily_distance(activities, group):
    acts = _group_members(activities, group)
    if not acts:
        return pd.Series([], dtype=float, name="distance_km")
    idx = pd.DatetimeIndex([a.date for a in acts])
    return pd.Series([a.distance_km for a in acts], index=idx).groupby(level=0).sum().sort_index()
```
- [ ] **Step 4: Run tests — expect PASS**
- [ ] **Step 5: Add `resample_agg` (W→iso_week Monday, ME→month), keep rolling/wow working on its output**
- [ ] **Step 6: Run full test_volume.py — expect PASS**
- [ ] **Step 7: Commit** `git add metrics/volume.py tests/test_volume.py && git commit -m "refactor(volume): daily source series + resample helper"`

## Task 2: Pipeline emits daily volume series (+ prune legacy weeklies)

**Files:**
- Modify: `pipeline.py`
- Modify: `store.py` (use existing `prune_legacy_keys`)
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: `volume.daily_distance`, `volume.daily_duration_hours`, `volume.resample_agg`
- Produces: daily `volume.distance_{group}_daily`, `volume.duration_{group}_daily` rows; session computes weekly/monthly on request.
- Migration: in `run_pipeline`, after writing new rows, `store.prune_legacy_keys([...old weekly volume keys...])` so stale weekly rows don't linger.

- [ ] **Step 1: Update `compute_metric_rows` to emit daily series instead of weekly**
```python
# pipeline.py, in the volume block
for group in volume.groups():
    daily_km = volume.daily_distance(activities, group)
    rows += rows_from_series(f"volume.distance_{group}_daily", daily_km, "computed",
                             params={"agg": "daily", "unit": "km"})
    daily_h = volume.daily_duration_hours(activities, group)
    rows += rows_from_series(f"volume.duration_{group}_daily", daily_h, "computed",
                             params={"agg": "daily", "unit": "h"})
```
- [ ] **Step 2: Update `test_pipeline.py::test_end_to_end_writes_metrics`** — change assertions: `store.read_metric("volume.distance_total_daily")` and `store.read_metric("volume.duration_total_daily")` instead of weekly keys.
- [ ] **Step 3: Add prune call in `run_pipeline`** (after `persist_session_metrics` writes, before close) — prune legacy `volume.distance_*`, `volume.duration_*`, `volume.rolling4wk_*`, `volume.wow_pct_*` keys. Use `store.prune_legacy_keys([...])`.
- [ ] **Step 4: Run `test_pipeline.py` — expect PASS** (fix assertions as needed).
- [ ] **Step 5: Commit** `git add pipeline.py store.py tests/test_pipeline.py && git commit -m "refactor(pipeline): emit daily volume; prune legacy weekly rows"`

## Task 3: Session resamples daily → weekly/monthly in `windowed`

**Files:**
- Modify: `session.py`
- Test: `tests/test_session.py`

**Interfaces:**
- Consumes: `series` (SessionView) with `volume.*_daily` keys; `profile.selected_volume_agg`
- Produces: `SessionView` gains `volume_agg` property + `windowed(...)` returns resampled (W or ME) series for `*_daily` metrics, leaving all non-volume metrics untouched.

- [ ] **Step 1: Add `SessionView.volume_agg: str` property** reading from `profile.selected_volume_agg`.
- [ ] **Step 2: In `SessionView.windowed`, for any metric ending in `_daily`, resample per `volume_agg`**:
```python
# session.py: inside windowed()
freq = {"weekly": "W", "monthly": "ME"}.get(self.volume_agg, "W")
if metric.endswith("_daily"):
    s = window_series(s.resample(freq).sum().sort_index(), since, until)
else:
    s = window_series(s, since, until, weekly=...)
```
- [ ] **Step 3: Update `test_session.py::test_weekly_volume_indexed_to_monday`** → rename to `test_volume_resampled_to_monday` and check default "weekly" resamples daily to Monday-indexed series preserving total. Add a parallel `test_volume_monthly_resample` using `profile.selected_volume_agg="monthly"`.
- [ ] **Step 4: Run `tests/test_session.py` — expect PASS**
- [ ] **Step 5: Commit** `git add session.py tests/test_session.py && git commit -m "feat(session): resample daily volume in windowed() per profile selection"`

## Task 4: Dashboard — remove rollup pane, add toggle, persist selection

**Files:**
- Modify: `profile.py`
- Modify: `dashboard.py`
- Modify: `store.py` (add `selected_volume_agg` to save/load)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- `RunnerProfile.selected_volume_agg: str = "weekly"` (add field).
- Sidebar radio initialized from persisted profile, persisted via `_persist_selected_fields`-style path (extend the existing helper to also save `selected_volume_agg`).
- `render_volume_tab` receives `selected_volume_agg`, labels charts "Weekly"/"Monthly" accordingly, and the WoW captions flip wording. No rollup table/pane.

- [ ] **Step 1: Add `selected_volume_agg` to `profile.py` RunnerProfile** (default "weekly").
- [ ] **Step 2: Add `selected_volume_agg` to `store.save_runner_profile` / `load_runner_profile`** (mirror `selected_race`).
- [ ] **Step 3: Extend `_persist_selected_fields` in dashboard.py** to persist `selected_volume_agg` (rename to `_persist_selected_profile_fields` or add a second call — keep it one helper that saves both `selected_race` and `selected_volume_agg`).
- [ ] **Step 4: Add sidebar radio** "Volume aggregation: Weekly / Monthly" (key `volume_agg`), initialized from persisted profile value, default weekly. Place it near the existing KPI multiselect / period selector so it survives the same rerun lifecycle.
- [ ] **Step 5: Delete the rollup pane block** (lines ~1222-1239: the `freq_label = st.radio(...)` + `rollup(...)` + `st.dataframe`) from `render_volume_tab`.
- [ ] **Step 6: Update `render_volume_tab` signature** to accept `selected_volume_agg: str` and pass it through; update the markdown bullet (remove "Rollups = ..." line; replace with "Aggregation: switch weekly/monthly via the sidebar toggle; weekly rolls up ISO weeks (Mon–Sun), monthly to calendar months.").
- [ ] **Step 7: Thread the value** — `main()` reads `profile.selected_volume_agg`, passes to `render_volume_tab(..., profile.selected_volume_agg)`; the radio writes back to `selected_volume_agg` and persists.
- [ ] **Step 8: Update chart titles/caption** conditionally (e.g. "Weekly distance" → f"{'Weekly' if agg=='weekly' else 'Monthly'} distance"). WoW captions already say "week-over-week" — guard with the label.
- [ ] **Step 9: Update `tests/test_dashboard.py`** — the existing rollup-table test (the one added in `4fdd2e1`) must be deleted; add a test that the volume aggregation radio exists and defaults to weekly and persists; extend `_FakeSt` if needed (no real Streamlit).
- [ ] **Step 10: Run `tests/test_dashboard.py` — expect PASS**
- [ ] **Step 11: Commit** `git add profile.py store.py dashboard.py tests/test_dashboard.py && git commit -m "feat(dashboard): weekly/monthly toggle on volume charts; remove rollup pane; persist selection"`

## Task 5: Migrate existing users' DBs + full verification

**Files:**
- (DB migration at runtime — `run_pipeline`/`load` path)
- Test: full suite

- [ ] **Step 1: Add a migration in `MetricStore`** (or `run_pipeline`) that calls `prune_legacy_keys` for the old weekly volume keys on startup/load, so the dashboard doesn't show stale weekly bars alongside daily ones. Use the existing `prune_legacy_keys(rows: list[str]) -> int`.
  ```python
  LEGACY_VOLUME_KEYS = [
      "volume.distance_total","volume.distance_running","volume.distance_treadmill",
      "volume.distance_cross","volume.duration_total","volume.duration_running",
      "volume.duration_treadmill","volume.duration_cross","volume.duration_strength",
      "volume.rolling4wk_total","volume.wow_pct_total", ... (all 5 groups × 3 series = 15)
  ]
  ```
- [ ] **Step 2: Remove `session.rollup` + `best_mile`?** — **NO.** `best_mile` is still used by the activities tab (best-mile stats) and is NOT volume. Keep it. Only `rollup` is removed (it was only used by the deleted pane). Remove `rollup` from `session.py` and from the `from session import (... rollup ...)` import in `dashboard.py`; delete the stale age-grading-brief text mentioning rollups.
- [ ] **Step 3: Add/remove dashboard imports** — `rollup` import removed; `render_activities` already uses `best_mile` which stays.
- [ ] **Step 4: Run full suite `pytest` — expect all green** (expect test_pipeline `test_compute_metric_rows_matches_run_pipeline` needs updating since weekly rows no longer persist — the comparison dict will have changed keys; update assertions to compare daily keys).
- [ ] **Step 5: Run `ruff check .` + `ruff format --check .` on touched files — expect clean.**
- [ ] **Step 6: Commit any test/migration fixes** `git add -A && git commit -m "fix: update equality tests for daily volume; add legacy-key migration"`

## Cross-reference matrix (what each tab sees)
| Tab | Metric keys consumed | Monthly-toggle affected? |
|---|---|---|
| Load & Recovery | `load.banister_*`, `pmc.ctl/atl/tsb`, `load.acwr`, `injury.*` | NO (already daily) |
| Fitness | `load.decoupling*`, `fitness.vo2max`, `age_graded.pct`, `race_*`, `load.lt_*` | NO (already daily or ingested) |
| Volume & Terrain | `volume.distance_*_daily`, `volume.duration_*_daily` (resampled), `volume.wow_pct_*_daily` (resampled) | **YES** |
| Activities | `fastest_split_1609`, `avg_speed`, etc. (per-activity, no agg) | NO |

## Rollback / revert notes
- Old weekly rows are pruned; if a user reverts this commit, `_daily` series won't recompute and volume charts will read empty — acceptable (regenerated on next Refresh).
- `selected_volume_agg` defaults to "weekly" if absent in an old profile (store.load returns default), so legacy users get weekly — matching prior behavior.
