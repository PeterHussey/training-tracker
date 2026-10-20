# Service Layer + Volume Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the six "Now" items from `docs/reviews/2026-10-20-status.md`: fix monthly volume aggregation, label the weekly/monthly bucket spill, build the service layer as one unit (Streamlit config + launchd + shared chart template), switch SQLite to WAL with a busy timeout, fix the README install step, and deliver laps/splits + running dynamics + stored `avg_cadence`.

**Architecture:** The metric layer is pure and already verified — no changes to metric *definitions*, only to bucket *geometry* (label month buckets at month start) and window *filtering* (keep every bucket that overlaps the window). The service layer is new surface: one `.streamlit/config.toml`, two launchd plists under `deploy/launchd/`, and one new `chart_theme.py` module that owns the palette, the Plotly layout template and the modebar config. Garmin laps/splits come from `/activity-service/activity/{id}/splits`, a real endpoint that returns `lapDTOs` with full running dynamics — so criterion 7 is met as written and the brief needs no amendment.

**Tech Stack:** Python 3.11+, pandas, plotly, streamlit, pytest, ruff, mypy, SQLite (WAL), launchd (macOS).

**Spec:** `PROJECT.md` (brief) + `docs/reviews/2026-10-20-status.md` (findings). No new design doc — the status review *is* the spec for this work.

## Global Constraints

- Metrics must stay derivable from Garmin data. No invented values.
- `.venv/bin/python -m pytest -p no:cacheprovider -q` must stay green (533 tests at plan time).
- `.venv/bin/python -m ruff check .` and `.venv/bin/python -m ruff format --check .` must stay clean for touched files.
- Streamlit must bind `127.0.0.1` only (brief constraint: "local-first: Streamlit dashboard bound to 127.0.0.1").
- No inline hex colour literals may remain in `dashboard.py` (criterion 13).
- Secrets are never written to disk by this work; the launchd plists read the existing `~/.garmin-mcp/v2_tokenstore.json`.
- `data/training.sqlite` is the live store; all DB-changing work must be exercised on a copy, never on the live file, except the WAL switch itself (which is a journal-mode change, not a data change).

## Review Focus

Five input classes the spec implies that no existing test exercises:

1. **A window ending mid-month** (e.g. `until=2026-08-27`) must still yield the August bucket — the exact defect the review found. Highest priority: it is the bug being fixed.
2. **A window entirely inside one month** (e.g. `2026-08-14..2026-08-16`) must yield exactly one bucket, not zero.
3. **A window in a month with no activity at all** must yield an empty series, not a zero-filled bucket.
4. **A launchd plist with a stale path** (repo moved, venv deleted) must fail loudly in the job's stdout log rather than silently doing nothing — the job must have `StandardOutPath`/`StandardErrorPath` under `logs/`.
5. **A Garmin splits payload missing a lap field** (e.g. no `averageRunCadence`, or no `lapDTOs` at all) must render as "—" / an empty table, not raise.

---

### Task 1: Monthly bucket labelling and window-overlap filtering

**Files:**
- Modify: `metrics/volume.py` (add `bucket_span`, document the `MS` convention)
- Modify: `session.py` (`window_series` gains a `freq` parameter; `SessionView.windowed` passes it; add `partial_bucket_labels`)
- Test: `tests/test_volume.py`, `tests/test_session.py`

**Interfaces:**
- Produces: `volume.bucket_span(index: pd.DatetimeIndex, freq: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]` — `(first_day, last_day)` of each bucket; `session.window_series(s, since, until, freq: str | None = None) -> pd.Series`; `session.partial_bucket_labels(s, since, until, freq) -> list[str]`.

- [x] **Step 1: Write the failing tests**

In `tests/test_session.py`:

```python
def test_monthly_bucket_keeps_in_progress_month():
    """A window ending mid-month must still show that month's bucket.

    The monthly buckets are labelled at month start and kept when they
    overlap the window, so the in-progress month is never dropped.
    """
    profile = RunnerProfile(
        hrmax=180, hrrest=60, sex="M", birth_year=1986, selected_volume_agg="monthly"
    )
    view = build_session_view(_acts(), profile, _lt(), _race(), _vo2())
    w = view.windowed(date(2026, 8, 1), date(2026, 8, 27))
    monthly = w["volume.distance_total_daily"]
    assert len(monthly) == 1
    assert monthly.index[-1] == pd.Timestamp("2026-08-01")
```

plus: a window fully inside one month yields exactly one bucket; a month with no activity yields none; `partial_bucket_labels` returns the edge buckets only.

- [x] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_session.py -k monthly -q`
Expected: FAIL — the August bucket is dropped when the window ends mid-month.

- [x] **Step 3: Implement**

In `metrics/volume.py`:

```python
def bucket_span(index: pd.DatetimeIndex, freq: str) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    """(first day, last day) of each bucket.

    Weekly buckets are Monday–Sunday; monthly buckets run from the 1st to the
    last day of the month. Unknown freqs are point buckets.
    """
```

In `session.py`, change `window_series(s, since, until, weekly=False)` to
`window_series(s, since, until, freq: str | None = None)`; when `freq` is set,
keep every bucket whose span overlaps `[since, until]` (start <= until AND
end >= since); otherwise keep rows whose own date is inside the window.
`SessionView.windowed` maps `"weekly" -> "W-SUN"`, `"monthly" -> "MS"` and
passes `freq` through; ISO-week metric rows pass `freq="W-SUN"`.

- [x] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_session.py tests/test_volume.py -q`
Expected: PASS.

- [x] **Step 5: Update the tests that pinned month-end labels**

`tests/test_volume.py::test_resample_monthly`, `tests/test_session.py::test_volume_daily_resampled_to_monthly`,
`::test_volume_monthly_index_is_month_end`, `::test_windowed_explicit_volume_agg_overrides_profile`
assert month-end index labels; they now assert month-start labels.

- [x] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -p no:cacheprovider -q`
Expected: PASS.

- [x] **Step 7: Commit**

```bash
git add metrics/volume.py session.py tests/test_session.py tests/test_volume.py
git commit -m "fix: label monthly volume buckets at month start and keep overlapping buckets"
```

---

### Task 2: SQLite WAL + busy timeout

**Files:**
- Modify: `store.py` (`MetricStore.__init__`)
- Test: `tests/test_store.py`

**Interfaces:**
- Produces: every `MetricStore` connection opens with `journal_mode=WAL` and a non-zero `busy_timeout`.

- [x] **Step 1: Write the failing test**

```python
def test_store_uses_wal_and_busy_timeout(tmp_path):
    store = MetricStore(tmp_path / "t.sqlite")
    try:
        assert store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
    finally:
        store.close()
```

- [x] **Step 2: Run it to verify it fails** — Expected: FAIL (`journal_mode` is `delete`).

- [x] **Step 3: Implement** — in `MetricStore.__init__`, after `sqlite3.connect(...)`:

```python
self.conn.execute("PRAGMA journal_mode=WAL")
self.conn.execute("PRAGMA busy_timeout=5000")
```

WAL lets the dashboard read while the scheduled refresh writes; the busy timeout
turns a lock collision into a short wait instead of an immediate "database is locked".

- [x] **Step 4: Run it to verify it passes** — Expected: PASS.

- [x] **Step 5: Run the full suite** — Expected: PASS.

- [x] **Step 6: Commit**

```bash
git add store.py tests/test_store.py
git commit -m "feat: open SQLite in WAL mode with a busy timeout"
```

---

### Task 3: README install step

**Files:**
- Modify: `README.md`

- [x] **Step 1: Rewrite the Quickstart** so step 1 is the install step and the
  rest of the steps work verbatim on a clean clone:

```markdown
## Quickstart

1. Install: `uv sync --all-extras` (creates `.venv`; `--all-extras` pulls the
   dev extra with pytest/ruff/mypy — `uv sync` alone installs runtime deps only).
2. Run tests: `.venv/bin/python -m pytest -p no:cacheprovider -q`
3. Launch dashboard: `.venv/bin/streamlit run dashboard.py`
4. DB location: `data/training.sqlite` (gitignored; override with `TRAINING_DB` env var). Garmin auth uses `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback (`GARMIN_EMAIL`/`GARMIN_PASSWORD`).
```

- [x] **Step 2: Verify on a clean clone**

```bash
git clone <repo> /tmp/tt_clone && cd /tmp/tt_clone && uv sync --all-extras && .venv/bin/python -m pytest -p no:cacheprovider -q
```
Expected: PASS.

- [x] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add the missing install step to the README quickstart"
```

---

### Task 4: Service layer as one unit

**Files:**
- Create: `.streamlit/config.toml`
- Create: `chart_theme.py`
- Create: `deploy/launchd/com.trainingtracker.dashboard.plist`
- Create: `deploy/launchd/com.trainingtracker.refresh.plist`
- Modify: `dashboard.py` (every chart through the shared helper; volume-tab bucket caption)
- Test: `tests/test_chart_theme.py`, `tests/test_service_config.py`

**Interfaces:**
- Consumes: `session.partial_bucket_labels` (Task 1).
- Produces: `chart_theme.PALETTE` (named colours), `chart_theme.style(fig, **overrides) -> go.Figure`, `chart_theme.render(fig, **kwargs) -> None` (calls `st.plotly_chart` with the shared config).

- [x] **Step 1: Write the failing tests**

`tests/test_chart_theme.py`: `style()` sets the shared template/margins/hovermode and leaves caller overrides intact; `PALETTE` has no duplicates; the three legacy hexes appear only in `chart_theme.py`.

`tests/test_service_config.py`: `.streamlit/config.toml` parses and pins `server.address = "127.0.0.1"`; both plists exist, carry `Label`, `ProgramArguments`, `RunAtLoad`, `StandardOutPath`, `StandardErrorPath`, and the refresh plist has a `StartCalendarInterval`.

- [x] **Step 2: Run them to verify they fail** — Expected: FAIL (no files yet).

- [x] **Step 3: Create `chart_theme.py`** — the single owner of colour and layout:

```python
"""Shared visual template for every dashboard chart.

One palette, one layout template, one Plotly config: `dashboard.py` must not
carry its own hex literals or hand-tuned layouts.
"""

ORANGE = "#F18F01"
BLUE = "#2E86AB"
MAGENTA = "#A23B72"
PALETTE = {"orange": ORANGE, "blue": BLUE, "magenta": MAGENTA}
PLOTLY_CONFIG = {"displayModeBar": False}
```

`style(fig, **overrides)` applies the shared layout and merges caller overrides;
`render(fig, **kwargs)` calls `st.plotly_chart(fig, config=PLOTLY_CONFIG, **kwargs)`.

- [x] **Step 4: Create `.streamlit/config.toml`** — `[server] address = "127.0.0.1"`,
  `[browser] gatherUsageStats = false`, and a `[theme]` block whose primary colour
  is the palette orange.

- [x] **Step 5: Create the two launchd plists** under `deploy/launchd/`:
  - `com.trainingtracker.dashboard` — runs `streamlit run dashboard.py`, `server.address=127.0.0.1`, `RunAtLoad`.
  - `com.trainingtracker.refresh` — runs a refresh entry point on a daily calendar interval, `RunAtLoad` off.
  Both write logs to `logs/` and reference the repo path via an absolute path
  placeholder that the install step rewrites.

- [x] **Step 6: Refactor `dashboard.py`** — replace all 12 inline hex literals
  with `chart_theme.PALETTE[...]`, route every `fig.update_layout(...)` through
  `chart_theme.style(...)`, and every `st.plotly_chart(...)` through
  `chart_theme.render(...)`. Add the volume-tab caption that names the bucket
  convention and the partial buckets (from `session.partial_bucket_labels`).

- [x] **Step 7: Verify**

```bash
grep -n "#[0-9A-Fa-f]\{6\}" dashboard.py    # must print nothing
.venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/python -m ruff check .
```

- [x] **Step 8: Commit**

```bash
git add .streamlit/config.toml chart_theme.py deploy/launchd dashboard.py tests/test_chart_theme.py tests/test_service_config.py
git commit -m "feat: shared chart template, Streamlit config and launchd jobs"
```

---

### Task 5: Garmin laps/splits, running dynamics and stored avg_cadence

**Files:**
- Modify: `garmin_http.py` (`fetch_activity_splits`)
- Modify: `gateway.py` (`fetch_activity_splits`, cached)
- Modify: `normalize.py` (`avg_cadence` already parsed — keep)
- Modify: `store.py` (`avg_cadence` column + migration + save/load)
- Modify: `dashboard.py` (laps table + running dynamics for the selected activity; cadence column)
- Test: `tests/test_store.py`, `tests/test_garmin_http.py`, `tests/test_dashboard.py`

**Interfaces:**
- Produces: `GarminHttp.fetch_activity_splits(activity_id: int) -> dict` returning
  `{"activityId": int, "lapDTOs": [...], "eventDTOs": [...]}`;
  `GarminGateway.fetch_activity_splits(activity_id)` with the same disk-cache
  discipline as `fetch_activity_details`.

- [x] **Step 1: Write the failing tests** — laps parsing (a lap maps to
  distance/duration/HR/cadence/GCT/stride/vertical oscillation, missing fields
  become `None`), the store round-trips `avg_cadence`, the dashboard renders a
  laps table for a selected activity.

- [x] **Step 2: Run them to verify they fail** — Expected: FAIL.

- [x] **Step 3: Implement the fetch** — `GET /activity-service/activity/{id}/splits`
  (verified live: returns `lapDTOs` with `distance`, `duration`, `movingDuration`,
  `elapsedDuration`, `averageHR`, `maxHR`, `averageRunCadence`, `maxRunCadence`,
  `averageSpeed`, `elevationGain`, `strideLength`, `groundContactTime`,
  `groundContactBalanceLeft`, `verticalOscillation`, `verticalRatio`,
  `intensityType`, `lapIndex`, `startTimeGMT`).

- [x] **Step 4: Store `avg_cadence`** — add the column, an idempotent `ALTER TABLE`
  migration, and save/load plumbing.

- [x] **Step 5: Display** — a laps table (lap #, distance, time, pace, HR,
  cadence, elevation) plus a running-dynamics summary for the selected activity;
  add a cadence column to the activity list.

- [x] **Step 6: Verify**

```bash
.venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

- [x] **Step 7: Commit**

```bash
git add garmin_http.py gateway.py normalize.py store.py dashboard.py tests/
git commit -m "feat: show Garmin laps, running dynamics and stored cadence"
```
