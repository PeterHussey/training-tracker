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