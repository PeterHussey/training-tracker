# Plan: RPE/Feel + weather + fastest-30s in Activities list & comparison

**Date:** 2026-10-02
**Status:** approved by owner (brainstorming spike → bounded design → "go")

## Context

The training tracker's Activities tab (`dashboard.render_activities`) and the
repetition-comparison view (`dashboard.render_repetitions`) should gain three
new data points:

1. **RPE + Feel** — the owner's watch-side self-evaluation. Verified live:
   `GET /activity-service/activity/{id}` returns `summaryDTO.directWorkoutRpe`
   (0–100, equals user RPE ÷ 10) and `summaryDTO.directWorkoutFeel`
   (enum: 0=Very Weak, 25=Weak, 50=Normal, 75=Strong, 100=Very Strong).
   NOT present in the activity-list payload.
2. **Weather** — verified live: `GET /activity-service/activity/{id}/weather`
   returns `temp`, `apparentTemp`, `dewPoint`, `relativeHumidity`, `windSpeed`,
   `windDirectionCompassPoint`, `weatherTypeDTO.desc`, `weatherStationDTO`.
   Sample was °F/mph (US station). Garmin-native → no Open-Meteo, no brief
   change. garminconnect exposes this as `get_activity_weather` (same
   `/activity-service/activity/{id}` base, suffix `/weather`).
3. **Fastest 30s** — computed from the existing details cache via
   `metrics/threshold.py::best_window(hr, speed, 30, ts_ms=...)`, which already
   implements the time-domain sliding window used by the 20/30-min LTHR
   anchors. On-demand for the selected comparison group only (owner's choice),
   never precomputed for all activities.

Owner confirmed scales: render RPE as `N/10`, Feel as its five labels
(Very Weak → Very Strong).

## Global Constraints

- Garmin-derivable only (PROJECT.md brief) — weather must come from Garmin's
  own endpoint; Open-Meteo is off the table (owner approved this course after
  the spike found Garmin-native weather).
- Fetch layer: `garmin_http.py` hard deadlines (`GET_DEADLINE_SECONDS`), no
  bare `requests` calls elsewhere; gateway wrappers cache to
  `cache/app_cache/` with the `_cache` pattern; failures skip, never block
  refresh.
- Storage: `store.py` `SCHEMA` + idempotent `_migrate` ALTER TABLE pattern;
  `Activity` dataclass gains None-able fields; `save_activities` /
  `_activity_from_row` round-trip; fingerprint reload must pick up value
  rewrites.
- UI: `—` when a value is absent (older rows before backfill, indoor runs,
  Garmin without weather). Temperature unit: store raw °F (`wx_temp_f` etc.)
  and convert at render using the existing units toggle (km → °C, miles → °F);
  verify against one metric-account activity before finalizing — if Garmin
  returns °C for metric accounts, key columns must store the raw value with
  unit recorded rather than assuming °F.
- Feel values outside {0,25,50,75,100} render as the raw value with a `?`
  suffix, never raise.
- Tests + lint gates: `.venv/bin/python -m pytest -p no:cacheprovider -q`,
  `.venv/bin/python -m ruff check .`, `.venv/bin/python -m ruff format --check .`.
- Conventions: Ruff line-length 100, E501 ignored, quote-style preserve.
  Comments only where the existing file's style comments.

## Tasks

### Task 1 — Gateway + HTTP fetch (new Garmin endpoints)

- `garmin_http.py`: add `fetch_activity_summary(activity_id)` →
  `GET /activity-service/activity/{id}` and
  `fetch_activity_weather(activity_id)` → `GET /activity-service/activity/{id}/weather`.
  Same `_threaded_get` deadline/retry pattern as existing calls (they go
  through `get_json`, which already applies it — so these are thin methods
  calling `self.get_json(...)`).
- `gateway.py`: add `fetch_activity_summary(activity_id)` and
  `fetch_activity_weather(activity_id)` wrappers that cache to
  `activity_summary_{id}.json` / `activity_weather_{id}.json`, mirroring
  `fetch_activity_details`.
- Parse helpers (in `normalize.py` or a small new module — prefer
  `normalize.py` since it owns raw→model): map `summaryDTO.directWorkoutRpe`
  ÷10 → 0–10 float; `summaryDTO.directWorkoutFeel` → label via
  {0: "Very Weak", 25: "Weak", 50: "Normal", 75: "Strong", 100: "Very Strong"}
  (unknown value → f"{raw}?"); weather fields → flat dict
  `{temp, apparent_temp, dew_point, humidity, wind_speed, wind_dir, desc}`.
  Missing/None-safe throughout (endpoint can return `{}` or lack fields).
- Tests: `tests/test_gateway.py` additions — cache round-trip for both new
  wrappers, malformed/empty payloads yield None-safe parses, unit tests for
  the RPE/Feel/weather mapping (include the out-of-range Feel `?` case).

### Task 2 — Store + normalize persistence

- `store.py` `SCHEMA` + `_migrate` (ALTER TABLE, idempotent, same guard
  style as `name`/`has_intervals`/`elapsed_s`): `rpe_10 REAL`, `feel TEXT`,
  `wx_temp REAL`, `wx_feels REAL`, `wx_humidity REAL`, `wx_wind REAL`,
  `wx_wind_dir TEXT`, `wx_desc TEXT`.
  (`wx_temp` stores degrees as returned by Garmin — the °F/°C ambiguity is
  resolved by Task 1's probe note in the ledger; record unit in a
  `wx_unit TEXT` column if the probe shows non-°F data.)
- `normalize.py`: extend `Activity` dataclass with the same fields, all
  defaulting `None`.
- `store.save_activities` / `store._activity_from_row`: round-trip all new
  columns (INSERT OR REPLACE gains the new column list).
- `store.activities_fingerprint`: unchanged (column-set + count + elapsed
  total already changes on migration and value rewrites).
- Tests: `tests/test_store.py` + `tests/test_normalize.py` — round-trip
  incl. NULLs, migration on a pre-change database (create old-schema table,
  open store, assert columns exist and values survive).

### Task 3 — Refresh plumbing (dashboard)

- `dashboard.refresh_garmin`: after `save_activities`, backfill
  summary+weather for the newly fetched activities only — cache-first
  (skip ids with `activity_summary_{id}.json` present), per-call timeout via
  `run_with_timeout`, skip-on-failure so refresh never blocks; then merge
  fetched fields into the `Activity` objects before they hit the store (order
  matters: backfill BEFORE `store.save_activities`, or re-save after).
- Tests: extend the dashboard test fakes (`tests/test_dashboard.py` /
  `tests/test_dashboard_period_reset.py`) with `fetch_activity_summary` /
  `fetch_activity_weather` stubs; assert refresh persists the new columns.

### Task 4 — Fastest-30s on demand

- New helper (in `session.py`): `fastest_30s_pace(series_by_id, activity_id)
  -> float | None` — calls `metrics.threshold.best_window(hr, speed, 30,
  ts_ms=ts)` on the cached details series, returns `mean_speed` (m/s) or None.
  Pace formatted via existing `_fmt_pace_min`.
- `dashboard.render_repetitions`: when a workout row is selected, ensure
  details are present for every rep of that code (fetch missing with
  `run_with_timeout` + cache, like `_fetch_series_for` but scoped to the
  group), then add a fastest-30s pace column per rep.
- Tests: `tests/test_threshold.py` — synthetic 1 Hz + timestamp series with a
  known fastest 30 s; dropout/gap tolerance; short-activity → None.
- Note: `best_window` requires valid samples spanning ≥ `window_s -
  max_gap`; a 30 s window with live 1–7 s sampling behaves like the 20/30-min
  path (adaptive gap), reuse as-is, no changes to `threshold.py`.

### Task 5 — Activities list + comparison UI

- `dashboard.render_activities`: add columns `RPE` (`6/10`), `Feel`
  (label), `Fast30s` (pace, on-demand only if cheap — otherwise omit from the
  flat list and keep it in comparison only; see ruling below), `Temp`, `Hum`,
  `Wind`, `Wx` (desc). `—` when absent.
  - Ruling: the flat activities list does NOT compute fastest-30s (that
    would need per-row details fetches for every row = the precompute the
    owner rejected). `Fast30s` appears only in the repetition-comparison
    table (Task 4), where the group is small and on-demand fetching is
    bounded. The flat list shows RPE/Feel/Wx only.
- `session.repetition_rows` + `dashboard.render_repetitions`: carry the new
  fields into the comparison table; extend the performance-over-repetitions
  chart with a fastest-30s pace trace (dashed, third pace series).
- Backfill story: one historical refresh populates older rows; UI degrades to
  `—` until then.
- Tests: `tests/test_session.py` (`repetition_rows` includes new fields),
  dashboard row-building tests.

### Task 6 — Verify

- `.venv/bin/python -m pytest -p no:cacheprovider -q` (full suite)
- `.venv/bin/python -m ruff check .`
- `.venv/bin/python -m ruff format --check .`
- `.venv/bin/python -m mypy .` (AGENTS.md lists it)
- `e2e_report.py` still exits 0.
- Manual note for owner: refresh, select a coded workout, confirm new columns
  match Garmin Connect (RPE/Feel, weather, fastest-30s).

## Order

Sequential 1 → 2 → 3 → 4 → 5 → 6 (each builds on the last).

## Risks / flags

- Weather unit ambiguity (°F assumed from US-station sample) — resolve in
  Task 1 probe or record `wx_unit`.
- Summary endpoint adds 1 call/activity on first backfill (cache makes
  repeats free); backfill must be bounded to new activities.
- Weather endpoint may 404 for indoor/no-station activities — treat as
  empty dict, store NULLs.
