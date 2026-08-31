# v2 Training Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An interactive Streamlit + plotly dashboard that shows every v2 metric with interpretation context, customizable date range, and editable profile inputs (HRmax etc.) that recompute on change.

**Architecture:** A pure, testable session-view module (`v2/session.py`) computes all metric rows via the existing pipeline and organizes them into dated series + interpretation context; a thin Streamlit view (`v2/dashboard.py`) handles Garmin refresh, profile editing, date range, and plotly rendering. Persistent SQLite DB stores activities + profile (source of truth); computed series are never persisted.

**Tech Stack:** Python 3.11, pandas, plotly, streamlit, existing v2 measurement layer.

**Spec:** `docs/superpowers/specs/2026-08-30-v2-dashboard-design.md`

## Global Constraints

- Python >= 3.11. Venv lives at repo root `.venv` (`.venv/bin/python`).
- New dependencies ONLY: `plotly>=5.18`, `streamlit>=1.35` (added to `v2/pyproject.toml`).
- `v2/session.py` must NOT import streamlit. It may import pandas and the existing v2 modules only.
- `e2e_report.py` must keep these names importable from `e2e_report` (existing tests import them): `_fmt`, `week_start`, `in_period`, `filter_period`, `_safe_json`, `CONDITIONAL_METRICS`, `EXPECTED_METRICS`. Task 3 does this by re-exporting them from `session`.
- Persistent DB path default: `v2/data/training.sqlite`; env var `TRAINING_DB` overrides. `v2/data/` must be gitignored.
- Existing v2 test suite must stay green after every task. Run from `v2/`:
  `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
- Code style matches the existing v2 modules: module/function docstrings, no decorative inline comments.
- Every task ends with a commit. Commit messages use the repo's `feat(v2):`, `fix(v2):`, `refactor(v2):` style.

---

## File Structure

- **Create `v2/session.py`** — pure session-view builder: shares metric-catalog constants and formatting/filtering helpers with `e2e_report.py`, and wraps `pipeline.compute_metric_rows` into a `SessionView`.
- **Create `v2/dashboard.py`** — the Streamlit app (thin view).
- **Modify `v2/pipeline.py`** — extract `compute_metric_rows()`; `run_pipeline()` becomes a shim.
- **Modify `v2/store.py`** — add `load_activities()` and `load_runner_profile()`.
- **Modify `v2/e2e_report.py`** — import shared helpers from `session` instead of defining them.
- **Modify `v2/pyproject.toml`** — add `plotly`, `streamlit`.
- **Modify `v2/.gitignore`** — ignore `data/`.
- **Create `v2/tests/test_session.py`**; **Modify `v2/tests/test_store.py`**, `v2/tests/test_pipeline.py`.

`session.py` owns the shared metric catalog and helpers so the CLI report and the dashboard can never drift on formatting/filtering semantics:
`WEEK_RE`, `DAY_RE`, `week_start`, `in_period`, `filter_period`, `_safe_json`, `_hhmm`, `_pace_mi`, `KM_PER_MI`, `_fmt`, `EXPECTED_METRICS`, `CONDITIONAL_METRICS`, `window_series`, `SessionView`, `build_session_view`, re-export `compute_metric_rows`.

---

### Task 1: Extract `compute_metric_rows` from the pipeline

**Files:**
- Modify: `v2/pipeline.py` (split `run_pipeline`)
- Test: `v2/tests/test_pipeline.py`

**Interfaces:**
- Consumes: existing `run_pipeline` body (rows are built without needing the store).
- Produces: `pipeline.compute_metric_rows(activities, profile, lt_payload=None, race_payload=None) -> list[dict]` — each dict has keys `metric, date, value, source, params, flags` (values/params/flags as in `metric_series` rows today). Task 3's `session.py` calls this; `run_pipeline` keeps the same signature and return dict.

- [ ] **Step 1: Write the failing tests**

Append to `v2/tests/test_pipeline.py`:

```python
def test_compute_metric_rows_matches_run_pipeline(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((FIXTURE.parent / "lactate_threshold.json").read_text())
    race = json.loads((FIXTURE.parent / "race_predictions.json").read_text())

    rows = compute_metric_rows(acts, default_profile(age=40, hrrest=60, sex="M"),
                               lt, race)
    assert rows

    db = tmp_path / "m.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db),
                 lt_payload=lt, race_payload=race)
    store = MetricStore(str(db))
    stored = []
    for metric in {r["metric"] for r in rows}:
        stored += store.read_metric(metric)
    store.close()

    def norm(r):
        return (r["metric"], r["date"], r["value"], r["source"],
                r["params"], r["flags"])

    assert {norm(r) for r in rows} == {norm(r) for r in stored}


def test_compute_metric_rows_empty():
    rows = compute_metric_rows([], default_profile(age=40, hrrest=60, sex="M"))
    assert rows == []
```

Update the import line in `v2/tests/test_pipeline.py`:

```python
from pipeline import compute_metric_rows, run_pipeline
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pipeline.py::test_compute_metric_rows_empty -q`
Expected: FAIL — `ImportError: cannot import name 'compute_metric_rows'`.

- [ ] **Step 3: Refactor `v2/pipeline.py`**

Change the function header and split as follows. Keep the module docstring and all imports. Replace lines 16-17 and move the row-building body into a new function; make `run_pipeline` a shim:

```python
def compute_metric_rows(activities: list[Activity], profile: RunnerProfile,
                        lt_payload: dict | None = None,
                        race_payload: dict | None = None) -> list[dict]:
    """Compute every metric row for a session view. Pure: no DB, no network.

    All other computation in this module builds on this; it must stay
    behavior-identical to what run_pipeline writes to the metric_series table.
    """
    rows = []

    # Volume — weekly distance (km), 4-wk rolling, week-over-week % (brief 4.1)
    for group in volume.groups():
        weekly = volume.weekly_distance(activities, group)
        rows += rows_from_series(f"volume.distance_{group}", weekly, "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.rolling4wk_{group}", volume.rolling_4wk(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.wow_pct_{group}", volume.week_over_week_pct(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "fraction"})

    # Elevation — context only (brief 4.2)
    gain = elevation.daily_elevation_gain(activities, "running")
    rows += rows_from_series("elevation.daily_gain_running", gain, "computed", params={"unit": "m"})
    rows += rows_from_series("elevation.rolling28d_running", elevation.rolling_28d(gain), "computed",
                             params={"unit": "m"})
    rows += rows_from_series("elevation.gain_per_km_running",
                             elevation.gain_per_km(activities, "running"), "computed",
                             params={"unit": "m/km"})

    # HR load + PMC + ACWR (brief 1.2, 1.3, 1.1)
    # Global Constraint: running metrics are computed on outdoor-`running`
    # activities only; treadmill/cycling/strength feed cross-training volume
    # (volume.groups()) only, never the HR-load anchors.
    RUNNING = [a for a in activities if a.sport == "running"]
    daily = trimp.daily_trimp(RUNNING, profile)
    if not daily.empty:
        ban = daily["banister"]
        # DENSIFY to calendar days (load 0 on rest days) so PMC/ACWR windows are
        # calendar windows, not active-day windows.
        ban = ban.reindex(pd.date_range(ban.index.min(), ban.index.max(), freq="D"), fill_value=0.0)
        rows += rows_from_series("load.banister",
                                 ban[ban > 0], "computed",
                                 params={"hrmax": profile.hrmax, "hrrest": profile.hrrest,
                                         "sex": profile.sex, "b": profile.banister_exponent()},
                                 flags={"hrmax_source": profile.hrmax_source})
        rows += rows_from_series("load.edwards", daily["edwards"], "computed")
        rows += rows_from_series("pmc", pmc.ctl_atl_tsb(ban), "computed",
                                 params={"tau_ctl": 42, "tau_atl": 7})
        acwr_s = acwr.coupled_acwr(ban)
        rows += rows_from_series("load.acwr", acwr_s, "computed",
                                 params={"acute": 7, "chronic": 28, "coupled": True})
        rows += rows_from_series("load.acwr_pct",
                                 acwr.history_percentile(acwr_s, window=180)["history_pct"], "computed",
                                 params={"window": 180})

    # Cross-training HR load (indoor bike, elliptical, strength...) — no distance,
    # so load is measured from HR only.
    CROSS = [a for a in activities if a.sport == "cross"]
    cross = trimp.daily_trimp(CROSS, profile)
    if not cross.empty:
        ban_cross = cross["banister"]
        rows += rows_from_series("load.banister_cross", ban_cross[ban_cross > 0], "computed",
                                 params={"hrmax": profile.hrmax, "hrrest": profile.hrrest,
                                         "sex": profile.sex, "b": profile.banister_exponent()},
                                 flags={"basis": "cross_training", "hrmax_source": profile.hrmax_source})
        rows += rows_from_series("load.edwards_cross", cross["edwards"], "computed",
                                 flags={"basis": "cross_training"})

    # VO2max — ingested reference (brief 2.1)
    rows += rows_from_series("fitness.vo2max", vo2max.daily_vo2max(activities), "garmin_ingested",
                             flags={"error_class": "firstbeat_estimate_5pct", "recompute": "no"})

    # CS (approximate critical speed) — computed from fastestSplit_1609 (brief 2.2)
    rows += rows_from_series("load.cs_approx", threshold.approx_cs_1609(activities), "computed",
                             params={"basis": "fastestSplit_1609", "unit": "m/s"})

    # LT + race predictions — ingested reference metrics, only when payloads supplied
    if lt_payload:
        lt = threshold.parse_lt(lt_payload)
        if lt.hr is not None and lt.date:
            rows += rows_from_series("load.lt_hr",
                                     pd.Series([float(lt.hr)],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "bpm"},
                                     flags={"anchored": "hr", "error_class": "lt_hr_7pct"})
        if lt.speed_m_s is not None and lt.date:
            rows += rows_from_series("load.lt_pace",
                                     pd.Series([lt.speed_m_s],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "m/s"},
                                     flags={"anchored": "no", "error_class": "lt_pace_over_20pct"})

    if race_payload:
        as_of = (race_payload.get("asOfDate") or race_payload.get("calendarDate")
                 or pd.Timestamp.today().strftime("%Y-%m-%d"))
        for dist, secs in racepredict.parse_predictions(race_payload).items():
            if secs is None:
                continue
            rows += rows_from_series(f"race_{dist}",
                                     pd.Series([float(secs)],
                                               index=pd.DatetimeIndex([pd.Timestamp(as_of)])),
                                     "garmin_ingested", params={"unit": "s", "distance": dist},
                                     flags={"error_class": "garmin_race_pred_maybe_optimistic"})

    return rows


def run_pipeline(activities: list[Activity], profile: RunnerProfile, out_db,
                 lt_payload: dict | None = None, race_payload: dict | None = None) -> dict[str, int]:
    store = MetricStore(out_db)
    store.save_runner_profile(profile)
    store.save_activities(activities)
    rows = compute_metric_rows(activities, profile, lt_payload, race_payload)
    store.save_metric_rows(rows)
    store.close()
    return {"metrics_written": len(rows), "activities": len(activities)}
```

The `run_pipeline` body in the current file (lines 16-121) must be replaced by this. The body moved verbatim into `compute_metric_rows`; do not change metric computation logic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pipeline.py tests/test_e2e_report.py -q`
Expected: PASS (both new tests and the pre-existing pipeline/CLI behaviors).

- [ ] **Step 5: Commit**

```bash
git add v2/pipeline.py v2/tests/test_pipeline.py
git commit -m "refactor(v2): extract compute_metric_rows for dashboard session views"
```

---

### Task 2: Store loaders for activities and profile

**Files:**
- Modify: `v2/store.py` (add `load_activities`, `load_runner_profile`)
- Test: `v2/tests/test_store.py`

**Interfaces:**
- Consumes: existing `MetricStore` schema and `save_*` methods; `normalize.Activity`; `profile.RunnerProfile`.
- Produces:
  - `MetricStore.load_activities() -> list[Activity]` — reconstructs `Activity` from stored columns (zone JSON parsed back to `{int: float}`; `ts_ms=0`, `elapsed_s=0.0`, no lat/lon/location/device_id — those fields are not persisted).
  - `MetricStore.load_runner_profile() -> RunnerProfile | None` — `None` when no profile row exists.
  - `Activity` import must live at module level in `store.py`; `RunnerProfile` import at module level too (no circular import: neither `normalize` nor `profile` imports `store`).

- [ ] **Step 1: Write the failing tests**

Append to `v2/tests/test_store.py` (the `date` import already exists at the top):

```python
from profile import RunnerProfile


SECOND_ACT = Activity(
    activity_id=2, sport="cross", date=date(2026, 4, 2), ts_ms=0,
    distance_m=0.0, duration_s=2700.0, elapsed_s=2700.0, avg_hr=140.0,
    max_hr=160.0, zone_s={1: 400.0, 2: 800.0}, vo2max=None, ele_gain_m=None,
)


def test_load_activities_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_activities([ACT, SECOND_ACT])
    loaded = store.load_activities()
    store.close()
    assert len(loaded) == 2
    first, second = loaded[0], loaded[1]
    assert first.activity_id == 1
    assert first.sport == "running"
    assert first.date == date(2026, 4, 1)
    assert first.distance_m == 10000.0
    assert first.duration_s == 3600.0
    assert first.avg_hr == 150.0
    assert first.max_hr == 170.0
    assert first.vo2max == 46.0
    assert first.ele_gain_m == 50.0
    assert first.zone_s == {1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0}
    assert second.sport == "cross"
    assert second.avg_hr == 140.0
    assert second.vo2max is None


def test_load_activities_empty(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_activities() == []
    store.close()


def test_load_runner_profile_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = default_profile(age=40, hrrest=60, sex="M")  # age-predicted hrmax 180 + zones
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == p.hrmax == 180
    assert loaded.hrmax_source == "age_predicted"
    assert loaded.hrrest == 60
    assert loaded.sex == "M"
    assert loaded.birth_year == p.birth_year
    assert loaded.hr_zones == p.hr_zones
    assert loaded.units == "metric"


def test_load_runner_profile_configured_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    p = RunnerProfile(hrmax=185, hrrest=62, sex="F", birth_year=1990, lthr_manual=168,
                      hrmax_source="configured")
    store.save_runner_profile(p)
    loaded = store.load_runner_profile()
    store.close()
    assert loaded is not None
    assert loaded.hrmax == 185
    assert loaded.hrmax_source == "configured"
    assert loaded.sex == "F"
    assert loaded.birth_year == 1990
    assert loaded.lthr_manual == 168


def test_load_runner_profile_none(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    assert store.load_runner_profile() is None
    store.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_store.py -q`
Expected: FAIL — `AttributeError: 'MetricStore' object has no attribute 'load_activities'`.

- [ ] **Step 3: Implement the loaders in `v2/store.py`**

Add `from datetime import date` to the existing imports, and `from normalize import Activity` and `from profile import RunnerProfile` at module level (after the existing imports). Append methods to `MetricStore` (before `close`):

```python
    def load_activities(self) -> list[Activity]:
        rows = self.conn.execute(
            "SELECT activity_id, activity_date, sport, distance_m, duration_s, "
            "ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, anaerobic_te, "
            "avg_speed, fastest_split_1609, zone_s FROM activities "
            "ORDER BY activity_date, activity_id"
        ).fetchall()
        return [self._activity_from_row(r) for r in rows]

    @staticmethod
    def _activity_from_row(row) -> Activity:
        (activity_id, activity_date, sport, distance_m, duration_s, ele_gain_m,
         vo2max, avg_hr, max_hr, aerobic_te, anaerobic_te, avg_speed,
         fastest_split_1609, zone_s) = row
        zones: dict[int, float] = {}
        if zone_s:
            try:
                zones = {int(k): float(v) for k, v in json.loads(zone_s).items()}
            except (json.JSONDecodeError, TypeError, ValueError):
                zones = {}
        return Activity(
            activity_id=int(activity_id),
            sport=sport,
            date=date.fromisoformat(activity_date),
            ts_ms=0,
            distance_m=float(distance_m),
            duration_s=float(duration_s),
            elapsed_s=0.0,
            zone_s=zones,
            ele_gain_m=ele_gain_m,
            vo2max=vo2max,
            avg_hr=avg_hr,
            max_hr=max_hr,
            aerobic_te=aerobic_te,
            anaerobic_te=anaerobic_te,
            avg_speed=avg_speed,
            fastest_split_1609=fastest_split_1609,
        )

    def load_runner_profile(self) -> RunnerProfile | None:
        rows = self.conn.execute(
            "SELECT key, value, meta FROM runner_profile"
        ).fetchall()
        if not rows:
            return None
        data = {k: v for k, v, _ in rows}
        hrmax_meta: dict = {}
        for k, v, m in rows:
            if k == "hrmax" and m:
                try:
                    hrmax_meta = json.loads(m)
                except json.JSONDecodeError:
                    hrmax_meta = {}
        zones: dict[int, tuple[int, int]] = {}
        raw_zones = data.get("hr_zones", "")
        if raw_zones:
            try:
                zones = {int(k): tuple(v) for k, v in json.loads(raw_zones).items()}
            except (json.JSONDecodeError, TypeError, ValueError):
                zones = {}
        lthr_raw = data.get("lthr_manual", "")
        return RunnerProfile(
            hrmax=int(data["hrmax"]),
            hrrest=int(data["hrrest"]),
            sex=data["sex"],
            birth_year=int(data["birth_year"]),
            lthr_manual=int(lthr_raw) if lthr_raw else None,
            hr_zones=zones,
            units=data.get("units", "metric"),
            hrmax_source=hrmax_meta.get("source", "configured"),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_store.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add v2/store.py v2/tests/test_store.py
git commit -m "feat(v2): store loaders for activities and runner profile"
```

---

### Task 3: `session.py` — shared helpers + session view builder

**Files:**
- Create: `v2/session.py`
- Modify: `v2/e2e_report.py` (import helpers from `session`; drop local copies)
- Test: `v2/tests/test_session.py`

**Interfaces:**
- Consumes: `pipeline.compute_metric_rows`, existing helpers currently defined in `e2e_report.py` (move those definitions here).
- Produces (all consumed by Task 5's `dashboard.py` and by `e2e_report.py` now):
  - `session.compute_metric_rows` (re-export)
  - `session.week_start(label: str) -> date | None`
  - `session.in_period(row_date: str, since: date) -> bool`
  - `session.filter_period(rows: list[dict], since: date) -> list[dict]`
  - `session._safe_json(text) -> dict`
  - `session._fmt(value: float, params: dict, units: str = "km") -> str`
  - `session.EXPECTED_METRICS: list[str]`
  - `session.CONDITIONAL_METRICS: dict[str, str]`
  - `session.SessionView` dataclass with fields `profile`, `series: dict[str, pd.Series]`, `context: dict[str, dict]`; method `windowed(since, until) -> dict[str, pd.Series]`; property `metrics -> list[str]`.
  - `session.build_session_view(activities, profile, lt_payload=None, race_payload=None) -> SessionView`
  - `session.window_series(s, since, until, weekly=False) -> pd.Series`

- [ ] **Step 1: Write the failing tests**

Create `v2/tests/test_session.py`:

```python
# tests/test_session.py
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from normalize import from_summary
from profile import default_profile
from session import build_session_view, in_period, week_start, window_series

FIXTURES = Path(__file__).parent / "fixtures"


def _acts():
    raw = json.loads((FIXTURES / "activities_sample.json").read_text())
    return [from_summary(a) for a in raw]


def _lt():
    return json.loads((FIXTURES / "lactate_threshold.json").read_text())


def _race():
    return json.loads((FIXTURES / "race_predictions.json").read_text())


def _view(**kw):
    profile = kw.pop("profile", default_profile(age=40, hrrest=60, sex="M"))
    return build_session_view(kw.pop("acts", _acts()), profile,
                              _lt(), _race())


REQUIRED = {
    "volume.distance_total", "volume.rolling4wk_total", "load.banister",
    "load.edwards", "pmc.ctl", "pmc.atl", "pmc.tsb",
    "load.banister_cross", "load.edwards_cross", "fitness.vo2max",
    "load.cs_approx", "load.lt_hr", "load.lt_pace",
    "race_5k", "race_10k", "race_half", "race_full",
}
# NOTE: load.acwr/load.acwr_pct are deliberately NOT in REQUIRED — the sample
# fixture's running span is 13d (< 28d chronic window), so coupled_acwr is
# all-NaN and nothing emits. e2e_report already asserts acwr presence ⇔
# running_span >= 28d (its conditional cross-check).


def test_session_view_emits_expected_metrics():
    view = _view()
    assert REQUIRED <= set(view.metrics)


def test_series_have_sorted_datetime_index():
    view = _view()
    for metric in view.metrics:
        s = view.series[metric]
        assert isinstance(s.index, pd.DatetimeIndex)
        assert s.index.is_monotonic_increasing


def test_weekly_volume_indexed_to_monday():
    view = _view()
    s = view.series["volume.distance_total"]
    assert (s.index.dayofweek == 0).all()
    assert view.context["volume.distance_total"]["params"]["agg"] == "iso_week"


def test_context_carries_params_and_flags():
    view = _view()
    ban = view.context["load.banister"]
    assert ban["params"]["hrmax"] == 180
    assert ban["flags"]["hrmax_source"] == "age_predicted"
    assert ban["source"] == "computed"
    vo2 = view.context["fitness.vo2max"]
    assert vo2["flags"]["error_class"] == "firstbeat_estimate_5pct"
    cross = view.context["load.banister_cross"]
    assert cross["flags"]["basis"] == "cross_training"


def test_windowed_filters_daily_series():
    view = _view()
    since, until = date(2026, 8, 20), date(2026, 8, 27)
    w = view.windowed(since, until)
    s = w["load.banister"]
    assert (s.index >= pd.Timestamp(since)).all()
    assert (s.index <= pd.Timestamp(until)).all()


def test_windowed_includes_week_overlapping_since():
    # 2026-W34 starts Mon 2026-08-17 and ends Sun 2026-08-23; since=08-20 is
    # inside it, so the row must be kept despite a Monday < since.
    view = _view()
    since, until = date(2026, 8, 20), date(2026, 8, 30)
    s = view.windowed(since, until)["volume.distance_total"]
    assert pd.Timestamp(week_start("2026-W34")) in s.index  # 2026-08-17
    assert pd.Timestamp(week_start("2026-W33")) not in s.index  # 2026-08-10


def test_higher_hrmax_lowers_banister_trimp():
    view_low = _view(profile=default_profile(age=40, hrrest=60, sex="M"))   # hrmax 180
    view_high = _view(profile=default_profile(age=25, hrrest=60, sex="M"))  # hrmax 195
    low, high = view_low.series["load.banister"], view_high.series["load.banister"]
    common = low.index.intersection(high.index)
    day = common[0]
    assert high.loc[day] < low.loc[day]


def test_empty_activities():
    view = build_session_view([], default_profile(age=40, hrrest=60, sex="M"))
    assert view.metrics == []


@pytest.mark.parametrize("label,start", [("2026-W34", date(2026, 8, 17)),
                                         ("2026-W33", date(2026, 8, 10))])
def test_week_start_parse(label, start):
    assert week_start(label) == start


def test_in_period_shared_semantics():
    since = date(2026, 7, 30)
    assert in_period("2026-08-20", since)
    assert not in_period("2026-07-01", since)
    assert in_period("2026-W34", since)
    assert not in_period("2026-W25", since)


def test_window_series_weekly_flag():
    idx = pd.DatetimeIndex([pd.Timestamp("2026-08-10"), pd.Timestamp("2026-08-17")])
    s = pd.Series([1.0, 2.0], index=idx)
    out = window_series(s, date(2026, 8, 20), date(2026, 8, 30), weekly=True)
    assert list(out.index) == [pd.Timestamp("2026-08-17")]
    assert list(out.values) == [2.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_session.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'session'`.

- [ ] **Step 3: Implement `v2/session.py`**

```python
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
```

- [ ] **Step 4: Update `v2/e2e_report.py` to import the shared helpers**

Remove the local definitions of `WEEK_RE`, `DAY_RE`, `week_start`, `in_period`, `filter_period`, `_safe_json`, `_hhmm`, `_pace_mi`, `KM_PER_MI`, `_fmt`, `EXPECTED_METRICS`, `CONDITIONAL_METRICS` (currently lines ~24-54 and ~141-182). Replace the imports block (currently lines 29-34) so it reads:

```python
import pandas as pd

from gateway import GarminGateway
from metrics import racepredict, threshold
from normalize import from_summary
from pipeline import run_pipeline
from profile import default_profile, with_estimated_hrmax
from session import (
    CONDITIONAL_METRICS,
    EXPECTED_METRICS,
    _fmt,
    _safe_json,
    filter_period,
    in_period,
    week_start,
)
from store import MetricStore
```

The rest of `e2e_report.py` stays unchanged; its `render`, `_main`, and tests keep working because the names are imported. Verify no name is still referenced that no longer exists by running the full suite.

- [ ] **Step 5: Run all tests to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: PASS (test_session.py + all pre-existing tests, including the `e2e_report` subprocess smoke).

- [ ] **Step 6: Commit**

```bash
git add v2/session.py v2/tests/test_session.py v2/e2e_report.py
git commit -m "feat(v2): session-view builder + shared metric helpers for dashboard"
```

---

### Task 4: Declare and install dashboard dependencies

**Files:**
- Modify: `v2/pyproject.toml`
- Modify: `v2/.gitignore`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `streamlit` and `plotly` importable in the venv; `v2/data/` ignored by git.

- [ ] **Step 1: Add dependencies**

In `v2/pyproject.toml`, change the `dependencies` list to:

```toml
dependencies = [
    "pandas>=2.2",
    "requests>=2.31",
    "garminconnect==0.3.2",
    "plotly>=5.18",
    "streamlit>=1.35",
]
```

- [ ] **Step 2: Ignore the data directory**

Append `data/` to `v2/.gitignore` (after the existing `cache/` line).

- [ ] **Step 3: Install**

Run: `../.venv/bin/pip install "streamlit>=1.35" "plotly>=5.18"`
Verify: `../.venv/bin/python -c "import streamlit, plotly; print(streamlit.__version__, plotly.__version__)"`

- [ ] **Step 4: Commit**

```bash
git add v2/pyproject.toml v2/.gitignore
git commit -m "build(v2): add streamlit and plotly for the dashboard"
```

---

### Task 5: `dashboard.py` — the Streamlit view

**Files:**
- Create: `v2/dashboard.py`

**Interfaces:**
- Consumes: `session.build_session_view`, `session.SessionView.windowed`, `session._fmt`, `session.CONDITIONAL_METRICS`; `store.MetricStore.load_activities`/`load_runner_profile`/`save_runner_profile`/`save_activities`; `gateway.GarminGateway`; `normalize.from_summary`; `profile.RunnerProfile`, `with_estimated_hrmax`.
- Produces: a launchable `streamlit run dashboard.py` app. Not unit-tested (thin view); verified by import + headless health check.

- [ ] **Step 1: Create `v2/dashboard.py`**

Write the full module (below). `if __name__ == "__main__": main()` guards execution so `python -c "import dashboard"` is safe (no Streamlit script run).

```python
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
from session import _fmt, build_session_view
from store import MetricStore

st.set_page_config(page_title="Training Tracker", layout="wide")

DB_PATH = os.environ.get("TRAINING_DB", str(Path(__file__).parent / "data" / "training.sqlite"))
FETCH_DAYS = 365
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


def profile_from_widgets(activities) -> RunnerProfile:
    sex = st.sidebar.selectbox("Sex", ("M", "F"))
    birth_year = st.sidebar.number_input("Birth year", min_value=1920,
                                         max_value=2100, value=1978)
    hrrest = st.sidebar.number_input("Resting HR (bpm)", min_value=30,
                                     max_value=120, value=60)
    src = st.sidebar.radio("HRmax source",
                           ("manual", "estimate from workouts", "age-predicted"))
    if src == "manual":
        default_hrmax = 220 - (date.today().year - int(birth_year))
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
    m_per_unit = 1000.0 if units == "km" else 1609.344
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
            label = f"Weekly {'mi' if units == 'miles' else 'km'}"
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
                                          title="rolling km"))
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
        fig = go.Figure(go.Bar(x=gain.index, y=gain.values, name="daily gain (m)"))
        roll28 = windowed.get("elevation.rolling28d_running")
        if roll28 is not None and len(roll28):
            fig.add_trace(go.Scatter(x=roll28.index, y=roll28.values,
                                     name="rolling 28d", line=dict(color="#F18F01")))
        per_km = windowed.get("elevation.gain_per_km_running")
        if per_km is not None and len(per_km):
            fig.add_trace(go.Scatter(x=per_km.index, y=per_km.values,
                                     name="m / km", yaxis="y2",
                                     line=dict(color="#2E86AB", dash="dash")))
            fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                          title="m / km"))
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
            m_per_unit = 1000.0 if units == "km" else 1609.344
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
    units = st.sidebar.radio("Units", ("km", "miles"))

    if "activities" not in st.session_state:
        st.session_state["activities"] = store.load_activities()
        st.session_state["lt_payload"] = None
        st.session_state["race_payload"] = None

    st.sidebar.header("Data")
    if st.sidebar.button("Refresh from Garmin"):
        try:
            acts, lt, race = refresh_garmin()
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
    profile = replace(profile_from_widgets(activities), units=units)
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

    view_sig = (activities_sig(activities), psig,
                st.session_state.get("lt_payload"), st.session_state.get("race_payload"))
    if st.session_state.get("_view_sig") != view_sig:
        st.session_state["view"] = build_session_view(
            activities, profile,
            st.session_state.get("lt_payload"), st.session_state.get("race_payload"))
        st.session_state["_view_sig"] = view_sig
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
```

- [ ] **Step 2: Verify the module imports cleanly**

Run: `cd v2 && ../.venv/bin/python -c "import dashboard; print('ok')"`
Expected: prints `ok` (module import must not execute the Streamlit script).

- [ ] **Step 3: Verify a headless launch comes up**

Run:

```bash
cd v2 && ../.venv/bin/streamlit run dashboard.py --server.headless true \
  --server.port 8511 >/tmp/tt_dash.log 2>&1 &
dash_pid=$!
sleep 8
curl -s http://localhost:8511/_stcore/health
kill $dash_pid
```

Expected: curl prints `ok`. The app on an empty/local DB renders the onboarding
message or the dashboard without crashing. Check `/tmp/tt_dash.log` for tracebacks
if anything fails.

- [ ] **Step 4: Run the full test suite**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add v2/dashboard.py
git commit -m "feat(v2): Streamlit dashboard with live Garmin refresh and profile editing"
```

---

### Task 6: Final verification

**Files:** none (verification only).

- [ ] **Step 1: Full suite + CLI report**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: PASS.

Run: `cd v2 && ../.venv/bin/python e2e_report.py --data offline --since 2026-08-01`
Expected: `E2E checks: PASS`, all `EXPECTED_METRICS` + conditional metrics listed.
Confirm `session` sharing did not change any reported values (spot-check a couple
of numbers against the same command before Task 3 if needed — the offline report
is deterministic).

- [ ] **Step 2: Manual dashboard smoke**

Run the Task 5 Step 3 headless launch again; expect a healthy `/_stcore/health`
response of `ok`. Confirm `git status` shows no untracked data files inside the
repo (`v2/data/` must be ignored).

- [ ] **Step 3: Document nothing beyond code**

No README/docs changes in this plan (module docstring carries the launch command).
Ensure `git status` is clean of unintended files (e.g. no `cache/`, `data/`,
`.DS_Store` staged).