# training-tracker

**Brief written:** 2026-09-10 (reconstructed after the fact — criteria post-date the code; intent elicited first, inventory second)
**Tier:** System

## Objective

Personal training dashboard based on Garmin data, improving on free available metrics and tools. For the owner's own running training.

## Success criteria

- [ ] Refresh pulls new Garmin data without errors — verify by: dashboard refresh completes within the 90s cap; `test_dashboard.py` / `test_gateway.py` pass
- [ ] All charts show valid Garmin-derived statistics after refresh, no plausible filler — verify by: visual spot-check against Garmin Connect (no NaNs, no invented values); `e2e_report.py` exits 0
- [ ] Date-window adjustment propagates across all charts — verify by: select a period in the UI and observe every chart updates to that window
- [ ] Selecting an activity accurately shows all previous comparison activities — verify by: select an activity and confirm the comparison list matches route/distance history
- [ ] Full metric set (trimp, pmc, acwr, volume, vo2max, threshold, racepredict, gap, decoupling, elevation, injury, hrmax, plus cross/strength handling) stays Garmin-derivable with checks green — verify by: `.venv/bin/python -m pytest -p no:cacheprovider -q` passes (236 tests) and metric definitions match `docs/v2/metrics-spec.md`
- [ ] Support tooling runs end to end (e2e_report CLI, backfill_names/backfill_sports) — verify by: `e2e_report.py` and relevant backfill tests pass

## Out of scope

- Not building: social/sharing features — because Strava and others already cover this well
- Not building: coaching plan generation — because the target is analysis/reporting, not plan prescription
- Not building: non-Garmin data sources — because the constraint is Garmin-derivable metrics only (adopted divergence: existing code with no remembered intent, e.g. specs/plans docs, is adopted as supporting material, not product scope)

## Constraints

- Metrics must be derivable from Garmin data.
- Personal use only; local-first: Streamlit dashboard, SQLite at `data/training.sqlite` (override via `TRAINING_DB`), Garmin auth via `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback.
- Python 3.11+; `garminconnect==0.3.2` pinned with owned `garmin_http.py` timeout layer; dashboard fetches up to 365 days with a 90s hard timeout.
- No deadline — unbudgeted personal time.

## The "is done" bar

| | Looks done | Is done |
|---|---|---|
| For this project | Fake/plausible-looking filler data in charts | Valid reports computed from the user's actual Garmin data |

## How to run and verify

- Run: `.venv/bin/streamlit run dashboard.py`
- Test: `.venv/bin/python -m pytest -p no:cacheprovider -q`
- Report: `.venv/bin/python e2e_report.py`
- Fixtures or sample data: `tests/fixtures/` (activities_sample.json, lactate_threshold.json, race_predictions.json, vo2max_trend.json)

## Review cadence

- **Status review** (grade against this brief): every 5th working session, or weekly while active
- **Expansion review** (should this brief change): at milestones only — e.g. after each success criterion is met

## Review log

| Date | Type | Summary | File |
|---|---|---|---|
