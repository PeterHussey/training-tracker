# training-tracker

**Brief written:** 2026-09-10 (reconstructed after the fact — criteria post-date the code; intent elicited first, inventory second)
**Tier:** System

## Objective

Personal training dashboard based on Garmin data, improving on free available metrics and tools. For the owner's own running training only — run locally as a persistent service that refreshes itself from Garmin.

## Success criteria

- [ ] Refresh pulls new Garmin data without errors — verify by: dashboard refresh completes within the 90s cap; `test_dashboard.py` / `test_gateway.py` pass
- [ ] All charts show valid Garmin-derived statistics after refresh, no plausible filler — verify by: visual spot-check against Garmin Connect (no NaNs, no invented values); `e2e_report.py` exits 0
- [ ] Date-window adjustment propagates across all charts — verify by: select a period in the UI and observe every chart updates to that window
- [ ] Selecting an activity accurately shows all previous comparison activities — verify by: select an activity and confirm the comparison list contains every other stored activity carrying the same 80/20 plan workout code
- [ ] Full metric set (trimp, pmc, acwr, volume, vo2max, threshold, racepredict, gap, decoupling, elevation, injury, hrmax, plus cross/strength handling and the Runalyze VDOT families vdot.shape, marathon.shape_pct, prognosis.*) stays Garmin-derivable with checks green — verify by: `.venv/bin/python -m pytest -p no:cacheprovider -q` passes and metric definitions match `docs/v2/metrics-spec.md`
- [ ] Support tooling runs end to end (e2e_report CLI, backfill_names/backfill_sports) — verify by: `e2e_report.py` and relevant backfill tests pass
- [ ] Selecting an activity shows its Garmin detail — laps/splits and running dynamics where available — verify by: select an activity with laps in the dashboard and confirm the splits match Garmin Connect; detail-parsing tests pass
- [ ] A fresh clone installs and runs following the README alone — verify by: clean-clone quickstart succeeds and missing Garmin credentials surface a clear error, not a stack trace
- [ ] Age grading (WMA), weekly/monthly rollups, and best-mile stat are computed and displayed — verify by: age-grading tests pin WMA table lookups and interpolation against published calculator values; rollup and best-mile tests pass; dashboard renders the age-graded trend, rollup table, and best-mile stat
- [ ] The dashboard runs as a background local service, not a foreground terminal — verify by: `launchctl list | grep trainingtracker` shows the dashboard job loaded, and `curl -sf http://127.0.0.1:8501/_stcore/health` returns healthy after a fresh login with no terminal open
- [ ] Garmin data refreshes on a schedule with no user action, and an open dashboard reflects it — verify by: `launchctl kickstart -k gui/$(id -u)/com.trainingtracker.refresh` adds rows to `data/training.sqlite` with the dashboard closed; with the dashboard open, the same job increases the activity count in the sidebar caption without a manual rerun
- [ ] The scheduled refresh does not contend with a running dashboard's SQLite access — verify by: run the refresh job while the dashboard is open; both stay up and neither log contains "database is locked"
- [ ] Charts render through one shared visual template — verify by: no inline hex colour literals remain in `dashboard.py`, every chart is drawn through the shared template helper, and the app renders with the theme in `.streamlit/config.toml`

## Out of scope

- Not building: social/sharing features — because Strava and others already cover this well
- Not building: coaching plan generation — because the target is analysis/reporting, not plan prescription
- Not building: non-Garmin data sources — because the constraint is Garmin-derivable metrics only (adopted divergence: existing code with no remembered intent, e.g. specs/plans docs, is adopted as supporting material, not product scope)
- Not building: hosted or multi-user publishing — because Garmin has no public OAuth API (a hosted service would mean collecting users' Garmin credentials: liability, ToS exposure, MFA/Cloudflare breakage), Streamlit is a single-user in-memory model, and intervals.icu/Runalyze already subsume the feature set
- Not building: a non-Streamlit UI — because the brief's constraint names Streamlit, every reported friction (foreground process, manual refresh, visual finish) is fixable inside it, and a rewrite would invalidate the metric layer and its tests
- Not building: Docker/systemd/containerised or hosted deployment — because the use is single-user on macOS, where launchd is native and adds no daemon dependency
- Not building: Runalyze feature parity beyond the three ported lookups (activities browser, gear, week summary, training plans) — because the 2026-10-17 port's purpose was to remove three specific side-by-side lookups and that is met; the remaining parity items are the incumbent's job
- Not building: route/distance-similarity grouping for comparison activities — because comparison groups by the 80/20 plan workout code parsed from the activity name, which is complete and correct; route/distance cohorts were parked on 2026-09-12 and are a different question from "show me the same workout again"

## Constraints

- Metrics must be derivable from Garmin data.
- Personal use only; local-first: Streamlit dashboard bound to 127.0.0.1 and run as a launchd LaunchAgent, SQLite at `data/training.sqlite` (override via `TRAINING_DB`), Garmin auth via `~/.garmin-mcp/v2_tokenstore.json` with 1Password/env fallback.
- Python 3.11+; `garminconnect==0.3.2` pinned with owned `garmin_http.py` timeout layer; dashboard fetches up to 365 days with a 90s hard timeout.
- No deadline — unbudgeted personal time.

## The "is done" bar

| | Looks done | Is done |
|---|---|---|
| For this project | Fake/plausible-looking filler data in charts | Valid reports computed from the user's actual Garmin data |

## How to run and verify

- Run: `.venv/bin/streamlit run dashboard.py` (foreground); as a service: launchd jobs `com.trainingtracker.dashboard` and `com.trainingtracker.refresh` — check with `launchctl list | grep trainingtracker`, run the refresh on demand with `launchctl kickstart -k gui/$(id -u)/com.trainingtracker.refresh`
- Test: `.venv/bin/python -m pytest -p no:cacheprovider -q`
- Report: `.venv/bin/python e2e_report.py`
- Fixtures or sample data: `tests/fixtures/` (activities_sample.json, lactate_threshold.json, race_predictions.json, vo2max_trend.json)

## Review cadence

- **Status review** (grade against this brief): every 5th working session, or weekly while active
- **Expansion review** (should this brief change): at milestones only — e.g. after each success criterion is met

## Review log

| Date | Type | Summary | File |
|---|---|---|---|
| 2026-09-10 | Status | 2 of 6 criteria verified; live Garmin refresh and interactive UI not probed | [docs/reviews/2026-09-10-status.md](docs/reviews/2026-09-10-status.md) |
| 2026-09-12 | Expansion | Adopted 2 criteria (per-activity detail, self-hostable install), objective widened to self-hosted use by others; parked 5, declined 0 | [docs/reviews/2026-09-12-expansion.md](docs/reviews/2026-09-12-expansion.md) |
| 2026-09-12 | Expansion | Addendum: objective reverted to personal-only after incumbent assessment (intervals.icu/Runalyze subsume the feature set); publishing declined with reason; adopted motivation-views criterion (age grading, weekly/monthly rollups, best-mile) | [docs/reviews/2026-09-12-expansion.md](docs/reviews/2026-09-12-expansion.md) |
| 2026-10-20 | Expansion | Adopted 4 criteria (persistent service, scheduled refresh, SQLite concurrency, shared chart template); objective and constraint amended to local-service operation; declined 3 (non-Streamlit UI, containerised/hosted deployment, further Runalyze parity); parked 2 | [docs/reviews/2026-10-20-expansion.md](docs/reviews/2026-10-20-expansion.md) |
| 2026-10-20 | Status | 4 of 13 criteria verified; metric engine healthy (533 tests, live e2e PASS, stored-vs-Garmin 9/9); monthly rollup drops the current month and the service layer is unbuilt | [docs/reviews/2026-10-20-status.md](docs/reviews/2026-10-20-status.md) |
| 2026-10-20 | Status | 9 of 13 criteria verified after the service-layer build; monthly rollup fixed, laps and cadence delivered, launchd service live on 127.0.0.1; two credential paths still emit a traceback and age_graded is missing from the metric spec | [docs/reviews/2026-10-20-status-2.md](docs/reviews/2026-10-20-status-2.md) |
| 2026-10-20 | Expansion | Amended 2 criteria (dropped the stale test-count parenthetical, added the Runalyze VDOT families, reworded criterion 4 to the plan-code mechanism); declined 1 (naming the Garmin splits endpoint in criterion 7) and declined route/distance grouping with a reason; parked 5; removed the unused watchdog dependency | [docs/reviews/2026-10-20-expansion-2.md](docs/reviews/2026-10-20-expansion-2.md) |
| 2026-10-21 | Status | 13 of 13 criteria verified by executed evidence (up from 9); service, scheduled refresh and scheduled health check all live, the health verdict now visible in the dashboard sidebar; two Garmin Connect UI clauses and the fresh-login clause remain unprobeable headlessly | [docs/reviews/2026-10-21-status.md](docs/reviews/2026-10-21-status.md) |
