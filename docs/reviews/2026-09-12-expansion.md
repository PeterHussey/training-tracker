# Expansion Review — 2026-09-12

**Brief dated:** 2026-09-10
**Last status review:** 2026-09-10 (with 2026-09-11 update note)
**Milestone that prompted this:** Dashboard in daily use — owner refreshes it after daily runs; user-initiated review of possible directions.

## Headline

Daily use revealed that the expensive part of this project is not computing metrics but validating them, and that several recently added secondary metrics have not earned their place yet. Meanwhile the refresh/persistence pipeline and the normalized store make new views on existing data nearly free. The target moves modestly: deeper per-activity detail (cheap, descriptive) and self-hostable installability (owner believes the tool fills a gap other trackers don't); nothing was pruned and no new derived models were adopted.

## What building this taught us

**Assumptions that turned out wrong:**
- Metric usefulness: the newer/secondary metrics (GAP, decoupling, elevation, injury, HRmax) turned out less useful than expected once seen against the owner's own daily data.

**Harder than expected, and why:**
- Metric correctness and validation — getting formulas right is one thing, verifying them against spec and reality (ACWR across gaps, LT anchoring) is where the real cost sits. Because validation is the expensive part, carrying low-value metrics is pure cost.

**Surprisingly easy, or came for free:**
- New views on existing data — once an activity is normalized into the store, deriving a new chart or summary from it is cheap.
- Refresh/persistence pipeline — the owned HTTP layer + tokenstore auth + SQLite store made daily refresh and persistence a solved problem.

## Emergent capabilities

- A normalized, persisted activity history in SQLite (`store.py`, `normalize.py`) — a clean local dataset that makes rollups, per-activity detail views, and history comparisons nearly free.
- A reliable Garmin fetch layer (`garmin_http.py`, `gateway.py`) with auth fallback — pulling additional Garmin endpoints or activity-detail payloads is now incremental work, not a project. Detail-fetch plumbing already exists (`fetch_activity_details`, used for LTHR detail scoped to the selected period).
- A tested session-view + metric-series layer where adding a chart on existing data is a small, testable change.

## Adjacent problems noticed

- "Could this be useful for others?" — the owner could not find a tracker that does everything this does. Productization proper (multi-user, hosting, support) belongs to a different project; the part that belongs here is installability — a fresh clone should run from the README alone. Adopted in that narrow form.
- Recovery/health data (sleep, HRV, readiness) — raised and explicitly not pursued; the owner wants depth on running, not breadth into health.

## Candidate calls

| Idea | Why it is warranted, tracing to a specific learning above | Call |
|---|---|---|
| Prune/demote secondary metrics (GAP, decoupling, injury, elevation, HRmax) | Learning: secondary metrics disappointed + validation is the expensive part | Park in Later — owner wants to attempt improving their usefulness before pruning anything |
| Validate predictions/estimates against actual performances | Learning: validation was the hard part; the brief should reward it | Split — race-prediction validation alone only reports Garmin's error and is not useful without an adjustment layer (parked); LTHR calibration (e.g. against the 20-min best-effort anchor) parked as "useful at some point" |
| Per-activity running depth: laps/splits and running dynamics from Garmin activity details | "Go deeper on running" + new views on existing data are nearly free + detail-fetch plumbing already exists | Adopt now |
| Broaden activity comparison beyond 80/20 plan codes (flagged by status review) | Status-review flag; decision depends on whether the owner actually names activities with codes | Park in Later — revisit after more daily use shows which mechanism is needed |
| Weekly/monthly rollup training-log view | New views are cheap + daily after-run check-in usage — but weakest-justified; existing charts may already serve the check | Park in Later |
| Make the project useful for others | Adjacent problem noticed in daily use; narrow kernel (installability) is traceable, the rest (hosting, multi-user) contradicts local-first constraints | Adopt now — in the narrow self-hostable form only |

## Amendments made to the brief

- **Objective:** before: "Personal training dashboard based on Garmin data, improving on free available metrics and tools. For the owner's own running training." → after: "…Primarily for the owner's own running training, but installable and runnable by others for their own self-hosted use."
- **Criteria added:**
  - Selecting an activity shows its Garmin detail — laps/splits and running dynamics where available — verify by: select an activity with laps in the dashboard and confirm the splits match Garmin Connect; detail-parsing tests pass
  - A fresh clone installs and runs following the README alone — verify by: clean-clone quickstart succeeds and missing Garmin credentials surface a clear error, not a stack trace
- **Criteria removed or replaced:** none — no existing criterion was invalidated by the adoptions.
- **Is-done bar:** unchanged.
- **Out of scope added:** none — nothing was declined in this review.

## Later

- Prune/demote secondary metrics (GAP, decoupling, injury, elevation, HRmax) — deferred; owner wants to attempt improving their usefulness first.
- LTHR calibration (e.g. against the 20-min best-effort anchor already used as KPI fallback).
- Adjusted race-prediction layer — validation alone only reports Garmin's error; only worth building if paired with an adjustment model.
- Broaden the activity-comparison mechanism beyond 80/20 plan codes (route/distance-based similarity) — flagged by the 2026-09-10 status review.
- Weekly/monthly rollup training-log view.
- Schema versioning for the SQLite store (carried from the 2026-09-10 status review's Later bucket).
