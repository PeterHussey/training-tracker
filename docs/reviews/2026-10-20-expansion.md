# Expansion Review — 2026-10-20

**Brief dated:** 2026-09-10 (amended 2026-09-12, amended 2026-10-20)
**Last status review:** 2026-09-10 (with a 2026-09-11 update note) — 41 commits and a large port have landed since, so most criteria are ungraded against current code
**Milestone that prompted this:** the Runalyze VDOT/paces port landed (2026-10-17), the dashboard is in daily use, and the owner reported three operational frictions: the UI is not as finished as Runalyze, the terminal process must stay alive, and refreshing is manual.

## Headline

Building the tracker stopped being about metrics and became about operating it. The refresh pipeline — the part that was supposed to be fragile — is now the most hardened code in the repo, which is exactly what makes unattended refreshing safe; meanwhile manual-only refreshing turned out to be a *correctness* problem, not just an inconvenience, because neither refresh mode ever re-reads a stored activity and Garmin-side edits (race tags) therefore never propagate. The target moves from "a dashboard that computes metrics" to "a local service that keeps itself current and reads as one system". Four criteria adopted, three ideas declined with reasons, two parked.

## What building this taught us

**Assumptions that turned out wrong:**
- The Garmin fetch layer was expected to be the fragile part. It is the opposite — it carries a 90s hard cap, incremental and historical fetch modes, a deadline-bounded backfill with budget resume, cache-first detail fetching, and a store-driven catch-up pass. It is safe to run unattended.
- Manual refreshing was expected to be a convenience issue. It is a correctness issue: per the 2026-10-17 VDOT design (Decision 5), neither refresh mode revisits a stored activity, so a race tagged in Garmin Connect after ingestion is never re-read, the VO2max corrector stays dormant at 1.0, and the tracker's numbers silently diverge from Runalyze's. Recovering that needs a separate repair script (`backfill_event_types.py`) the owner must know to run.
- The dashboard was expected to be "a Streamlit app". It is a 2,118-line application with 21 metric modules, 40 test files, four tabs and ~16 charts, and it is in daily use — it outgrew a foreground dev server.

**Harder than expected, and why:**
- Scope definition in the Runalyze port, not the mathematics. The formulas were transcribable from PHP; deciding what to port, what to declare a non-goal, and which numbers to anchor against Runalyze's test tree was the cost.
- Deciding what "nice" means for a dashboard with no incumbent to copy. Runalyze's polish comes from one shared visual system; this dashboard has no theme config at all and hand-rolled chart styling per chart.

**Surprisingly easy, or came for free:**
- A headless refresh. `dashboard.py` has exactly one module-level `st.` call (`set_page_config`), so `import dashboard` works outside a Streamlit runtime and `refresh_garmin()` is callable from a plain script — no new fetch code, no new auth path.
- Persistence across process boundaries. The gateway already writes every fetch to `cache/app_cache/`, and `load_cached_trends()` rehydrates the trend payloads from disk, so a background refresh leaves the next app load fully populated.
- Background writes reaching an open app. `resolve_activities` reloads on a store fingerprint change (columns + row count + elapsed total), so rows written by another process are picked up on the next rerun with no change to the session layer.

## Emergent capabilities

- A UI-free refresh entry point — `refresh_garmin()` is a plain function in `dashboard.py`, importable headlessly, already wrapped in `run_with_timeout` and internally deadline-bounded.
- A disk-persisted trend cache (`cache/app_cache/`) that makes the dashboard restartable without a Garmin round-trip.
- A fingerprint-guarded store reload that makes the SQLite file, rather than the process, the source of truth for the open session.
- Metric computation fully decoupled from ingestion — nothing about scheduling a refresh touches the metric layer or its tests.

## Adjacent problems noticed

- `APP_CACHE_DIR = Path("cache") / "app_cache"` is a **relative** path. It resolves correctly from the repo directory and nowhere else, so any service, cron job, or launchd agent must set its working directory exactly, or it will write a second cache tree that the dashboard never reads. This is this project's problem (it breaks this project's service) and is folded into the service adoption.
- `watchdog` is declared in `pyproject.toml` but imported nowhere in the repo. A dependency left behind by an abandoned attempt to improve the dev-server experience.
- The SQLite store migrates in place with `ALTER TABLE` and no versioning (carried from the 2026-09-10 status review). A scheduled refresh means the migration can now run in two processes — mitigated as part of the concurrency criterion, but versioning itself remains unaddressed and stays in Later.
- Publishing for others was raised again by the "not as nice as Runalyze" comparison and is unchanged from the 2026-09-12 decision: declined.

## Candidate calls

| Idea | Why it is warranted, tracing to a specific learning above | Call |
|---|---|---|
| Run the dashboard as a persistent local service (launchd) instead of a foreground terminal | Learning: the app outgrew a foreground dev server (2.1k lines, 21 metric modules, 40 test files) and is in daily use; the owner reports the friction directly | Adopt now |
| Refresh Garmin data on a schedule with no user action | Learning: the fetch layer is hardened enough to run unattended, and manual-only refreshing is the root cause of the stale-race-tag correctness gap | Adopt now |
| Make an open dashboard reflect background writes without a manual rerun | Learning: `resolve_activities` reloads on a store fingerprint change, so a scheduled refresh is invisible until the session reruns | Adopt now (folded into the scheduled-refresh criterion) |
| Guarantee the scheduled refresh does not contend with a running dashboard's SQLite access | Learning: a second writer process is new; the store opens SQLite without WAL and migrates in place | Adopt now |
| One shared chart visual template plus a project theme | Learning: 16 charts carry 20 hand-rolled `update_layout` calls and 12 inline hex literals, none pass a `config=`, and there is no `.streamlit/config.toml` — the concrete source of "pretty good, not great" | Adopt now |
| Merge the Garmin-derived and Runalyze-derived panels into single comparison views (VO2max trend vs shape trend; race predictions vs prognosis table) | Learning: the port's layout decision was "distributed", and the result is two charts for one concept; reversing a recorded decision deserves a look at the rendered result | Park in Later |
| Port more Runalyze surfaces (activities browser, gear, week summary, training plans) | Learning: the port's stated purpose was to remove three specific side-by-side lookups and that is met; the remaining parity items were declared non-goals at design time | Decline |
| Replace Streamlit with another UI framework | Learning: every reported friction is fixable inside Streamlit; a rewrite would invalidate the metric layer and its tests | Decline |
| Docker/systemd/containerised or hosted deployment | Learning: the use is single-user on macOS, where launchd is native and needs no daemon | Decline |

## Amendments made to the brief

- **Objective:** before: "Personal training dashboard based on Garmin data, improving on free available metrics and tools. For the owner's own running training only." → after: "…For the owner's own running training only — run locally as a persistent service that refreshes itself from Garmin."
- **Criteria added:**
  - The dashboard runs as a background local service, not a foreground terminal — verify by: `launchctl list | grep trainingtracker` shows the dashboard job loaded, and `curl -sf http://127.0.0.1:8501/_stcore/health` returns healthy after a fresh login with no terminal open
  - Garmin data refreshes on a schedule with no user action, and an open dashboard reflects it — verify by: `launchctl kickstart -k gui/$(id -u)/com.trainingtracker.refresh` adds rows to `data/training.sqlite` with the dashboard closed; with the dashboard open, the same job increases the activity count in the sidebar caption without a manual rerun
  - The scheduled refresh does not contend with a running dashboard's SQLite access — verify by: run the refresh job while the dashboard is open; both stay up and neither log contains "database is locked"
  - Charts render through one shared visual template — verify by: no inline hex colour literals remain in `dashboard.py`, every chart is drawn through the shared template helper, and the app renders with the theme in `.streamlit/config.toml`
- **Criteria removed or replaced:** none — no existing criterion was invalidated by the adoptions. The existing refresh criterion ("Refresh pulls new Garmin data without errors") is not replaced; it still governs the correctness of a refresh, whether it is triggered by the button or by the schedule.
- **Is-done bar:** unchanged. The bar is about data validity ("valid reports computed from the user's actual Garmin data"), and nothing about scheduling or visual consistency changes what counts as valid data.
- **Constraint amended:** "Personal use only; local-first: Streamlit dashboard…" → "…Streamlit dashboard bound to 127.0.0.1 and run as a launchd LaunchAgent…". The 127.0.0.1 binding keeps the service local and consistent with the personal-use constraint; the dashboard job and the refresh job are the two launchd agents implied by the criteria.
- **Out of scope added:**
  - a non-Streamlit UI — declined because the brief's constraint names Streamlit, every reported friction (foreground process, manual refresh, visual finish) is fixable inside it, and a rewrite would invalidate the metric layer and its tests
  - Docker/systemd/containerised or hosted deployment — declined because the use is single-user on macOS, where launchd is native and adds no daemon dependency
  - Runalyze feature parity beyond the three ported lookups (activities browser, gear, week summary, training plans) — declined because the 2026-10-17 port's purpose was to remove three specific side-by-side lookups and that is met; the remaining parity items are the incumbent's job

## Later

- Merge the Garmin-derived and Runalyze-derived panels into single comparison views (VO2max trend vs shape trend; race predictions vs prognosis table) — reverses the recorded "layout B" decision; revisit after the shared chart template lands and the rendered result can be judged.
- Prune/demote secondary metrics (GAP, decoupling, injury, HRmax) — carried from the 2026-09-12 review; the owner wants to attempt improving their usefulness first. (Elevation is now load-bearing: the VDOT shape uses Runalyze's elevation correction.)
- LTHR calibration (e.g. against the 20-min best-effort anchor already used as KPI fallback).
- Adjusted race-prediction layer — validation alone only reports Garmin's error; only worth building if paired with an adjustment model.
- Broaden the activity-comparison mechanism beyond 80/20 plan codes (route/distance-based similarity) — flagged by the 2026-09-10 status review.
- Schema versioning for the SQLite store — carried from the 2026-09-10 status review; the scheduled refresh makes the in-place `ALTER TABLE` migration run in two processes, which the concurrency criterion mitigates but does not resolve.
- Remove the unused `watchdog` dependency — cleanup for the implementation pass, not a brief-level concern.
