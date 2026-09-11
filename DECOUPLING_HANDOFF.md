# Decoupling Implementation Handoff Prompt

## Objective
Wire the existing `metrics/decoupling.py` module into the pipeline, session, and dashboard so aerobic decoupling appears as a KPI in the dashboard with a trend chart.

## What Already Exists
- `metrics/decoupling.py` — `decoupling_percent`, `eligible_activity`, `route_key`, `aggregate_decoupling` with complete documentation and 100% unit test coverage (tests/test_decoupling.py)
- Full spec and field mapping in docs (v2/metrics-spec.md, running-metrics-brief.md, garmin-field-mapping.md)
- Garmin field mappings already defined (metrics[].heartRate, metrics[].speed)

## Scope
- **Per-sample series**: Consume per-second HR and speed from activity_details JSON (metrics[].heartRate[], metrics[].speed[]) as documented in the spec
- **Eligibility**: runs only (sport != "running" excluded), duration >=5400 s, gradient <=25 m/km, interval filtering via `hasIntensityIntervals` flag if implemented
- **Aggregation**: route-key clustered flat segments, >=6 sessions produces {"n", "mean", "stdev", "min_sessions_applied"}; fewer sessions -> null (not shown)
- **Dashboard**: add "load.decoupling" to KPI list (as a trend series, not a per-day value), with chart similar to PMC/ACWR (line + markers), clear on the sidebar, and expose it in the load/recovery tab

## Files to Modify
1. `pipeline.py` — import `metrics.decoupling` and add decoupling rows to `compute_metric_rows`
2. `session.py` — add "load.decoupling" to EXPECTED_METRICS and update dashboard route linking if needed
3. `dashboard.py` — add "Aerobic decoupling" to KPI_KEYS, implement `render_decoupling_tab` (or merge into load/recovery), ensure it filters with the period window, and conditionally shows if any data exists

## Integration Details

### Pipeline.py
- Import: `from metrics import decoupling`
- For each activity eligible by `eligible_activity(a)`:
  - Get series by activity_id from `series_by_id` (guaranteed to exist after details fetch; skip if missing)
  - From `(hr_series, speed_series, ts_ms_series)` normalize to common timestamps (avg over 60s blocks if needed)
  - Call `decoupling_percent(hr_list, speed_list)` producing a float
  - Append to `rows` via `rows_from_series` with metric name "load.decoupling", params unit "fraction", source "computed"
- Aggregate multiple runs via `aggregate_decoupling([decoupling_values])`; if >=6 sessions emit aggregated row alongside per-activity rows

### Session.py
- Add "load.decoupling" to EXPECTED_METRICS list
- The session view already handles it via rows_from_series (no other changes needed)

### Dashboard.py
- KPI_KEYS: append `("load.decoupling", "Aerobic decoupling")`
- Sidebar: include it in the KPI field multiselect (default visible)
- UI: new chart similar to PMC/ACWR, maybe as a sub-tab under Load & Recovery (or merge into the main load chart if space allows)
- If no data, display a brief help text like "Aerobic decoupling needs >=6 route-matched flat sessions"
- Show the context line (source/params/flags) on hover/caption

## Implementation Steps
1. Write decoupling tests for series consumption (new test file or extend existing test_decoupling.py) to verify hr/speed extraction and per-activity aggregation
2. Modify `pipeline.py` to import and compute decoupling per activity using series_by_id
3. Add to EXPECTED_METRICS in session.py
4. Update dashboard.py KPI list and implement decoupling chart rendering
5. Run existing tests to verify nothing else broke

## Testing Checklist
- [ ] Pipeline produces decoupling rows for eligible activities
- [ ] Session view aggregates per-activity series into correct trend series
- [ ] Dashboard renders decoupling KPI and chart when data present
- [ ] Graceful degradation when series missing or activities not eligible
- [ ] CI passes (ruff, mypy, pytest)

## Verification
- Smoke: launch dashboard locally (`.venv/bin/streamlit run dashboard.py`), refresh with fresh Garmin data (or a fixture), observe decoupling appears in KPI bar and trend chart
- CLI: test pipeline functions directly via python -m pytest tests/test_decoupling.py -v
- Lint: `.venv/bin/ruff check . && .venv/bin/mypy .`

## Dependencies
- No external packages beyond what's already in the environment (pandas, ruff, mypy, streamlit)
- No breaking changes to existing APIs

## Known Issues / Caveats
- The `hasIntensityIntervals` flag is currently ignored in `eligible_activity`; if needed, add that check before `eligible_activity` returns true
- Route clustering uses a static 0.01° grid; this is acceptable for the trend chart but note the spatial tolerance in documentation
- If HR/speed series are missing (e.g., optical HR gaps), skip that activity and log a warning
- Single-run values are dominated by day-to-day noise; the UI should only show aggregated trends (>=6 sessions)

---

This prompt outlines the minimal changes needed to wire the existing, documented decoupling logic into the pipeline and dashboard. If the activity_details series are not consumable in the existing workflow, the implementation may require adjusting the gateway or detail-fetching pipeline, but the core metric computation is already proven and tested.
