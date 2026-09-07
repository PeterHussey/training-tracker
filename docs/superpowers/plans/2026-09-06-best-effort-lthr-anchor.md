# Best-Effort LTHR Anchor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 30-day rolling-mean LTHR fallback with a best-sustained-window anchor (20-min × 0.95 and 30-min × 0.97) computed from activity-details series, outdoor-running only, displayed as anchor card + qualifier dots.

**Architecture:** Pure `metrics/threshold.py` extractor (details-series parse → sliding contiguous-window argmax → cross-activity best) fed by a pruned, cache-first details fetch; `pipeline.py` emits new anchor + dots keys and deletes the rolling keys; `dashboard.py`/`session.py` render cards + scatter.

**Tech Stack:** Python 3.11+, pandas (DatetimeIndex series), existing `GarminGateway.fetch_activity_details` (`maxChartSize=2000`, `activity_details_{id}.json` disk cache), SQLite via `rows_from_series` (`v2/metric_series.py:8`), Streamlit + plotly (existing).

**Spec:** This chat plan (bounded design approved 2026-09-06: both windows, outdoor-only, details ingest, replace rolling mean, display option c). No separate spec doc.

## Global Constraints

- Python 3.11+ (`X | Y` syntax allowed).
- Ruff line-length 100, `E501` ignored; new code must be `ruff check` + `ruff format --check` clean; run from `v2/` with `../.venv/bin/python`.
- `normalize.sport_of` is `typeKey`-only (`running` | `treadmill_running` → treadmill | `strength_training` → strength | else cross) — outdoor scope means `a.sport == "running"` exactly.
- Garmin LT ingest (`load.lt_hr`/`load.lt_pace`) and `RunnerProfile.lthr_manual` keep priority; best-effort is fallback-only (same gating as current `pipeline.py:231-234`).
- Dashboard 90s hard refresh timeout stands; details fan-out must never block it.
- `rows_from_series(metric, series, source, params, flags)` — params/flags JSON-serialized per row; dates `%Y-%m-%d`.

## File structure

- Modify `v2/metrics/threshold.py` — add `parse_details_series`, `best_window`, `best_effort_lthr` (+ dots collector); delete or deprecate `rolling_lt_hr`, `rolling_lt_pace`, `_sustained_efforts`.
- Modify `v2/pipeline.py:231-253` — replace rolling fallback block with anchor + dots emission.
- Modify `v2/dashboard.py:578-621` — replace rolling line charts with anchor cards + dots scatter; update fallback text.
- Modify `v2/session.py:41-58` — replace `load.lt_hr_rolling` conditionals with new keys + context strings.
- Modify `v2/tests/test_threshold.py` — replace rolling tests with best-window tests.
- Add fixture `v2/tests/fixtures/activity_details_1.json` (frozen minimal `metrics[]` series) + pipeline/dashboard tests touching new keys.
- Maybe modify refresh call-site in `dashboard.py` (details fan-out with cap/timeout) — confirm exact fetch loop location during Task 4.

---

### Task 1: Details-series parser + sliding best-window

**Files:**
- Modify: `v2/metrics/threshold.py`
- Test: `v2/tests/test_threshold.py`

**Interfaces:**
- Consumes: raw details dict (`{"metrics": [{"heartRate": float|None, "speed": float|None, ...}], ...}` — exact key names verified against a live `activity_details_{id}.json` cache file in Task 4; registry intent at `v2/garmin_fields.py:313-328`).
- Produces: `parse_details_series(details: dict, sample_s: float = 1.0) -> tuple[list[float|None], list[float|None]]` (hr, speed aligned); `best_window(hr, speed, window_s: int, sample_s: float = 1.0, max_gap_s: float = 5.0) -> dict | None` with keys `{mean_speed, mean_hr, start_idx}`; `None` when no fully-covered contiguous window exists.

- [ ] **Step 1: Write the failing test**

```python
def test_best_window_picks_fastest_contiguous_segment():
    hr = [150.0]*600 + [172.0]*1200 + [150.0]*600
    speed = [2.8]*600 + [3.8]*1200 + [2.8]*600
    w = best_window(hr, speed, window_s=1200)
    assert w["mean_speed"] == pytest.approx(3.8)
    assert w["mean_hr"] == pytest.approx(172.0)

def test_best_window_gap_breaks_contiguity():
    hr = [172.0]*1200
    speed = [3.8]*1200
    hr[600] = None  # optical gap > max_gap_s at 1Hz
    # with max_gap_s=0.5 and single missing sample, window covering it is invalid
    w = best_window(hr, speed, window_s=1200, max_gap_s=0.5)
    assert w is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: FAIL with `best_window not defined`.

- [ ] **Step 3: Write minimal implementation** — aligned-list parser (skip resample sophistication: assume isti 1Hz when `maxChartSize=2000` downsampling is uniform; document assumption), O(n) sliding sums over valid-mask, contiguity via gap counter.
- [ ] **Step 4: Run test to verify it passes**

Run: `../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/threshold.py v2/tests/test_threshold.py
git commit -m "feat: add details best-window extractor"
```

### Task 2: Cross-activity best + factors + outdoor-only scope

**Files:**
- Modify: `v2/metrics/threshold.py`
- Test: `v2/tests/test_threshold.py`

**Interfaces:**
- Consumes: `activities: list[Activity]`, `series_by_id: dict[int, tuple[hr, speed]]`, Task 1 functions.
- Produces: `best_effort_lthr(activities, series_by_id, window_s: int, factor: float) -> dict | None` → `{proxy_hr, raw_hr, pace, date, activity_id, window_s, factor}`; wrapper `best_effort_anchors(...) -> dict` returning `{"w20": ..., "w30": ..., "dots20": [...], "dots30": [...]}` where each dot is `{hr, pace, date, activity_id}`.

- [ ] **Step 1: Write the failing test**

```python
def test_best_effort_outdoor_only_and_factor():
    # treadmill + cross + short + hr-less candidates excluded; fastest outdoor 20-min wins; proxy = raw*0.95
    ...
    assert anchor["proxy_hr"] == pytest.approx(anchor["raw_hr"] * 0.95)
    assert anchor["activity_id"] == fastest_outdoor_id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: FAIL with `best_effort_lthr not defined`.

- [ ] **Step 3: Write minimal implementation** — filter `a.sport == "running" and a.duration_s >= window_s and a.avg_hr is not None and a.activity_id in series_by_id`; per-activity `best_window`; argmax on `mean_speed`; dots = every per-activity best (one dot per qualifier). Factors 0.95 (1200s) / 0.97 (1800s) applied at this layer, provenance preserved.
- [ ] **Step 4: Run tests**

Run: `../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/threshold.py v2/tests/test_threshold.py
git commit -m "feat: add cross-activity best-effort anchor with factors"
```

### Task 3: Pipeline emission swap (delete rolling, emit anchors + dots)

**Files:**
- Modify: `v2/pipeline.py:231-253`
- Test: `v2/tests/test_pipeline.py` (extend) + reuse `v2/tests/fixtures/activity_details_1.json` (added here if not present)

**Interfaces:**
- Consumes: Task 2 `best_effort_anchors`; `compute_metric_rows` gains optional `series_by_id: dict | None = None` (default None → no best-effort rows, preserves pure-activities path).
- Produces rows: `load.lt_hr_best20`, `load.lt_pace_best20`, `load.lt_hr_best30`, `load.lt_pace_best30` (single-point `pd.Series` at best date, `computed`, params `{unit, window_s, factor, basis: best_window_outdoor_running}`, flags `{activity_id, error_class: best_effort_estimate}`); `load.lt_effort_dots20_hr/_pace` (dots series). Deletes `load.lt_hr_rolling`, `load.lt_pace_rolling`, `rolling_lt_hr`, `rolling_lt_pace`, `_sustained_efforts`.

- [ ] **Step 1: Write the failing test**

```python
def test_pipeline_emits_best_not_rolling():
    rows = compute_metric_rows(acts, profile, lt_payload=None, series_by_id=series_by_id)
    metrics = {r["metric"] for r in rows}
    assert "load.lt_hr_best20" in metrics and "load.lt_hr_best30" in metrics
    assert "load.lt_hr_rolling" not in metrics and "load.lt_pace_rolling" not in metrics
    assert "load.lt_effort_dots20_hr" in metrics
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: FAIL (old keys present / new keys missing).

- [ ] **Step 3: Write minimal implementation** — keep Garmin-gating (`has_lt_hr/has_lt_pace`); when `series_by_id` present and anchors found, `rows_from_series` single-point + dots; remove rolling block.
- [ ] **Step 4: Run tests**

Run: `../.venv/bin/python -m pytest tests/test_pipeline.py tests/test_threshold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add v2/pipeline.py v2/metrics/threshold.py v2/tests/test_pipeline.py v2/tests/fixtures/activity_details_1.json
git commit -m "feat: emit best-effort anchors, remove rolling mean"
```

### Task 4: Cache-first pruned details fan-out (refresh path)

**Files:**
- Modify: refresh call-site in `v2/dashboard.py` (locate `fetch_activities` + `fetch_lactate_threshold` loop; thread through `series_by_id` into `compute_metric_rows`/`persist_session_metrics`).
- Test: manual + existing `tests/test_dashboard.py` (no new unit test if fetch is I/O; assert cap + timeout constants exist).

**Interfaces:**
- Consumes: `GarminGateway.fetch_activity_details`, disk cache `activity_details_{id}.json`.
- Produces: `series_by_id` for Task 3; constants `DETAILS_TOP_K = 40`, `DETAILS_TIMEOUT_S = 8`.

- [ ] **Step 1: Confirm live payload keys** — read one cached `activity_details_{id}.json` (or fetch one) and verify `metrics[].heartRate/speed` key names; adjust `parse_details_series` if keys differ (e.g. nested `metrics` vs flat).
- [ ] **Step 2: Implement pruned fan-out** — candidates sorted by `averageSpeed` desc, cache-hit first, bounded per-request, skip-on-miss/timeout, never exceed refresh budget.
- [ ] **Step 3: Verify** — `../.venv/bin/python -m pytest tests/test_dashboard.py -v` PASS + one live refresh smoke (anchor card appears when Garmin LT absent).
- [ ] **Step 4: Commit**

```bash
git add v2/dashboard.py
git commit -m "feat: pruned cache-first details fan-out for LTHR anchor"
```

### Task 5: Dashboard cards + dots, session context, cleanup

**Files:**
- Modify: `v2/dashboard.py:578-621`, `v2/session.py:41-58`
- Test: `v2/tests/test_dashboard.py`, `v2/tests/test_session.py`

**Interfaces:**
- Consumes: new row keys from Task 3.
- Produces: two anchor cards ("LTHR anchor 20-min: {proxy} bpm ({raw} @ {pace}, {date})" + 30-min) with faint dots scatter behind each; updated `CONDITIONAL_METRICS` strings; fallback text without "rolling 45-day".

- [ ] **Step 1: Write the failing test**

```python
def test_session_context_has_best_keys():
    ...build_session_view(acts, profile, series_by_id=...)
    assert "load.lt_hr_best20" in view.series
```

- [ ] **Step 2: Run to verify it fails**

Run: `../.venv/bin/python -m pytest tests/test_session.py tests/test_dashboard.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement rendering** — `st.metric`-style cards + `go.Scatter(mode="markers", opacity=0.35)` dots; pace dots converted via existing `pace_min_per_unit`; remove `rolling_threshes` block.
- [ ] **Step 4: Run full suite + lint**

Run: `../.venv/bin/python -m pytest -q`
Run: `../.venv/bin/python -m ruff check .`
Run: `../.venv/bin/python -m ruff format --check .`
Expected: all PASS / clean (pre-existing violations outside touched files out of scope).

- [ ] **Step 5: Commit**

```bash
git add v2/dashboard.py v2/session.py v2/tests/test_dashboard.py v2/tests/test_session.py
git commit -m "feat: anchor cards with qualifier dots for best-effort LTHR"
```

## Self-review

- Spec coverage: both windows ✓ (Tasks 1-3 x2), outdoor-only ✓ (Task 2 filter), details ingest ✓ (Tasks 1+4), replace rolling ✓ (Task 3 deletion + Task 5 UI), anchor + dots ✓ (Tasks 3+5).
- No placeholders: all steps carry concrete code/commands; Task 4 Step 1 explicitly resolves the one unknown (live details key names).
- Type consistency: `best_window → dict{mean_speed, mean_hr, start_idx}` feeds `best_effort_lthr → dict{proxy_hr, raw_hr, pace, date, activity_id, window_s, factor}` feeds single-point `pd.Series` + dots lists into `rows_from_series` — names match across tasks.
