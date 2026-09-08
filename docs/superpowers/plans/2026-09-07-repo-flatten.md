# Repo Flatten Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove deprecated `v1/` directory and flatten `v2/` to root in two commits.

**Architecture:** Two-commit approach: first remove v1 (preserving history), then move v2 contents to root and update all path references.

**Tech Stack:** Git, Python, ruff, mypy, pytest

**Spec:** `docs/superpowers/specs/2026-09-07-repo-flatten-design.md`

## Global Constraints

- Git history is preserved (`git rm` only removes from working tree)
- Python 3.11+ required
- Ruff configured with line-length 100
- `.venv/` stays at repo root (already there)
- Cache directories (`.mypy_cache`, `.pytest_cache`, `__pycache__`) are regenerated, not moved

---

### Task 1: Commit 1 — Remove v1 directory

**Files:**
- Delete: `v1/` (entire directory)
- Modify: `AGENTS.md:5-6` (remove v1 layout description)
- Modify: `docs/superpowers/plans/2026-08-30-v2-running-metrics-measurement.md:225` (clean `../v1/cache/` reference)

**Interfaces:**
- Consumes: Current repo state with `v1/` present
- Produces: Repo without `v1/`, updated docs

- [ ] **Step 1: Remove v1 directory**

```bash
git rm -r v1/
```

- [ ] **Step 2: Update AGENTS.md — remove v1 layout description**

Edit `AGENTS.md` lines 5-6, change:
```markdown
- **`v1/`** — Legacy Streamlit app. Uses `garminconnect-mcp` (stdio JSON-RPC via bun). Not actively developed.
- **`v2/`** — Active codebase. Own HTTP layer, metrics pipeline, Streamlit dashboard.
```
to:
```markdown
- **`v2/`** — Active codebase. Own HTTP layer, metrics pipeline, Streamlit dashboard.
```

- [ ] **Step 3: Update doc reference to v1 cache**

Edit `docs/superpowers/plans/2026-08-30-v2-running-metrics-measurement.md` line 225, change:
```python
raw = json.loads(Path("../v1/cache/garmin_raw.json").read_text())
```
to:
```python
# v1 cache removed — this was a historical reference
```

- [ ] **Step 4: Verify v2 still works**

Run: `cd v2 && ../.venv/bin/python -m pytest`
Expected: All tests pass (v2 is unchanged)

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "remove deprecated v1 directory"
```

---

### Task 2: Commit 2 — Flatten v2 to root

**Files:**
- Move to root: All Python files, `metrics/`, `tests/`, `pyproject.toml`, `data/`, `cache/`
- Delete: `.mypy_cache/`, `.pytest_cache/`, `__pycache__/`
- Modify: `AGENTS.md` (rewrite layout and commands sections)
- Modify: `backfill_sports.py:2`, `backfill_names.py:2`, `dashboard.py:3`, `e2e_report.py:3` (usage comments)

**Interfaces:**
- Consumes: Repo state after Task 1 (v1 removed, v2 still in subdirectory)
- Produces: Flat repo structure with all v2 contents at root

- [ ] **Step 1: Move Python files to root**

```bash
git mv v2/backfill_names.py .
git mv v2/backfill_sports.py .
git mv v2/dashboard.py .
git mv v2/e2e_report.py .
git mv v2/garmin_fields.py .
git mv v2/garmin_http.py .
git mv v2/gateway.py .
git mv v2/metric_series.py .
git mv v2/normalize.py .
git mv v2/pipeline.py .
git mv v2/profile.py .
git mv v2/session.py .
git mv v2/store.py .
```

- [ ] **Step 2: Move directories to root**

```bash
git mv v2/metrics .
git mv v2/tests .
git mv v2/pyproject.toml .
```

- [ ] **Step 3: Move gitignored directories**

```bash
git mv v2/data . 2>/dev/null || true
git mv v2/cache . 2>/dev/null || true
```

- [ ] **Step 4: Delete cache directories (don't move)**

```bash
rm -rf v2/.mypy_cache v2/.pytest_cache v2/__pycache__
rm -rf v2 2>/dev/null || true
```

- [ ] **Step 5: Update usage comments in Python files**

Edit `backfill_sports.py` line 2, change:
```python
"""Backfill activity sports from live Garmin data. Usage (from v2/):
```
to:
```python
"""Backfill activity sports from live Garmin data. Usage (from root):
```

Edit `backfill_names.py` line 2, change:
```python
"""Backfill activity names from live Garmin data. Usage (from v2/):
```
to:
```python
"""Backfill activity names from live Garmin data. Usage (from root):
```

Edit `dashboard.py` line 3, change:
```python
Run from v2/:
```
to:
```python
Run from root:
```

Edit `e2e_report.py` line 3, change:
```python
for a recent period. Usage (from v2/):
```
to:
```python
for a recent period. Usage (from root):
```

- [ ] **Step 6: Update AGENTS.md — rewrite layout section**

Replace entire `AGENTS.md` with updated version (see Appendix A below)

- [ ] **Step 7: Verify everything works from root**

Run: `../.venv/bin/python -m pytest`
Expected: All tests pass

Run: `../.venv/bin/python -m ruff check .`
Expected: No errors

Run: `../.venv/bin/python -m mypy .`
Expected: No errors

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "flatten v2 to root, remove version prefix"
```

---

## Appendix A: Updated AGENTS.md

```markdown
# AGENTS.md

## Repository layout

- **`metrics/`** — Pure computation modules (one per metric family). No DB, no network.
- **`tests/`** — Test suite with fixtures in `tests/fixtures/`.
- **`docs/`** — Specs, field mapping, research briefs. See `docs/v2/metrics-spec.md` for metric definitions and formulas.

## Commands

```bash
# Run the dashboard
.venv/bin/streamlit run dashboard.py

# Run all tests
.venv/bin/python -m pytest

# Run a single test file
.venv/bin/python -m pytest tests/test_trimp.py

# Run one test
.venv/bin/python -m pytest tests/test_trimp.py::test_name

# Lint
.venv/bin/python -m ruff check .

# Format check
.venv/bin/python -m ruff format --check .

# Type check
.venv/bin/python -m mypy .
```

## Architecture

**Data flow**: Garmin API → `gateway.py` → `normalize.py` (raw JSON → `Activity`) → `pipeline.py` (compute all metrics) → `store.py` (SQLite) → `session.py` (view builder) → `dashboard.py` (Streamlit UI)

- `garmin_http.py` — Owned REST layer with strict timeouts. Cloudflare-TLS stalls can hang indefinitely; each call has a hard thread-based deadline.
- `gateway.py` — Auth resolution: v2 tokenstore → owned HTTP → garminconnect fallback → 1Password/env creds.
- `profile.py` — `RunnerProfile` dataclass (HRmax, HRrest, sex, zones, Banister params).
- `metrics/` — Pure computation modules (one per metric family). No DB, no network.
- `metric_series.py` — Converts pandas output to metric_series rows.
- `store.py` — SQLite with tables: `runner_profile`, `activities`, `metric_series`.
- `session.py` — Pure: builds dated series + interpretation context for dashboard.
- `e2e_report.py` — CLI metric report. Flags exit code 1 if checks fail.

## Key conventions

- **Sport classification** is `typeKey`-only in `normalize.py`: `running` | `treadmill_running` | `strength_training` → `strength` | else `cross`. No `sportTypeId` fallback. Strength is duration-only (no HR load).
- **PMC/ACWR metrics** use outdoor-running activities only. Cross-training load is its own series, never double-counted.
- **Race predictions come from the daily-history endpoint** `/metrics-service/metrics/racepredictions/daily/{name}?fromCalendarDate=&toCalendarDate=` (full window, ~365 entries), NOT `/latest` (single snapshot). `gateway.fetch_race_predictions_trend` caches to `race_predictions_trend_{start}_{end}.json`; the chart plots per-distance time series and `store.load_runner_profile` persists the race-distance selector via `selected_race`.
- **Metrics are dotscored**: `volume.distance_total`, `load.banister`, `pmc.ctl`, etc.
- **Test fixtures** live in `tests/fixtures/` (activities_sample.json, lactate_threshold.json, race_predictions.json, vo2max_trend.json).
- **Python 3.11+** required (uses `X | Y` union syntax, `match` if needed).
- **Ruff** configured with line-length 100, quote-style preserve, `E501` ignored (formatter enforces).

## Gotchas

- The SQLite DB lives at `data/training.sqlite` (gitignored). Set `TRAINING_DB` env var to override.
- Garmin auth uses a persisted DI token (`~/.garmin-mcp/v2_tokenstore.json`). Credentials fallback: 1Password via `op` CLI → `GARMIN_EMAIL`/`GARMIN_PASSWORD` env vars.
- The dashboard fetches up to 365 days from Garmin on each refresh with a 90s hard timeout.
- `garminconnect` 0.3.2 is pinned — its own HTTP layer stalls on Cloudflare; that's why `garmin_http.py` exists.
```
