# Expansion Review — 2026-10-22

**Brief dated:** 2026-09-10
**Last status review:** 2026-10-21
**Milestone that prompted this:** the brief's criteria all verified for the first time, and the service layer that made them verifiable grew three capabilities the brief never mentions (a scheduled health check, a pre-write snapshot of the store, versioned schema migrations).

## Headline

The target should move in wording and in one added criterion, not in ambition: everything the brief asked for is built and verified, and what it missed is the operational layer that keeps the answer true — the service that checks itself and a store that can be restored. The metric spec also needed one honest sentence it never had, about which day an activity belongs to.

## What building this taught us

**Assumptions that turned out wrong:**
- A status review is something a person runs by hand. The probes were a dozen throwaway scripts outside the repo, so each review rediscovered the baseline from scratch. Once they lived in a file (`health_check.py`) and a launchd job, the review could start from a known state instead.
- "SQLite handles the concurrency" meant one connection could be shared. It could not: concurrent `execute()`/`fetchall()` on one `sqlite3.Connection` interleaves and returns duplicated or `None` rows, which surfaced as a `KeyError` in `load_runner_profile` and crashed the dashboard.
- A scheduled job that fails will be noticed. It was not: the refresh and the health check both died quietly until every entry point learned to report a missing Garmin credential as one line instead of a stack trace.
- One connection per thread is safe. It is not free: Streamlit gives every session its own thread and every fragment a pool thread, the store outlives both, and each abandoned connection holds two file descriptors — against launchd's 256-descriptor limit the dashboard eventually stops accepting connections. The connections now die with their threads.

**Harder than expected, and why:**
- Making the dashboard survive an absent Garmin credential took two entry points and two different surfaces (a sidebar warning and a stderr line), because the dashboard and the CLI have different audiences.
- The shared chart template's title/legend collision: the legend sat at a paper offset above 1.0, so it scaled with the figure's height, and the four call sites that still carried the old hand-tuned offset were what pushed it into the title.
- Reproducing the shared-connection corruption needed a 60-iteration loop — it fails roughly one run in sixteen, which is exactly the frequency a single test run cannot see.

**Surprisingly easy, or came for free:**
- The live store is a complete replica of production: the dashboard computes its view in memory from activities, so one `sqlite3.Connection.backup()` gives a dataset that exercises every real path.
- Garmin's `/splits` endpoint carries full running dynamics — cadence, ground-contact time, stride length, vertical oscillation — one request per activity, cached.
- A restorable store is one `backup()` call, so the scheduled refresh can take a snapshot before every write for the cost of a few milliseconds.

## Emergent capabilities

- `health_check.py` is a scheduled, machine-readable baseline: every probe a review would re-run, executable daily, its verdict surfaced in the dashboard sidebar, and its last line a single timestamped `PASS`/`FAIL`.
- `MetricStore.snapshot()` makes "what did the data look like yesterday" a one-line question, and makes every destructive probe safe against a copy.
- `PRAGMA user_version` plus numbered migrations turns "add a column" into a one-line, one-test change instead of a guess-the-schema `ALTER`.
- The Garmin payload cache holds hundreds of real activity payloads — a fixture corpus for anything that needs real Garmin shapes.

## Adjacent problems noticed

- The generated `docs/v2/garmin-field-mapping.md` still cites a path that moved when the repo was flattened (`v2/garmin_fields.py` → `garmin_fields.py`). Fixed in `f4dec98`; it will drift again unless the generator and the tree are kept in step.
- macOS launchd gives a LaunchAgent 256 file descriptors — a quarter of what an interactive shell gets. Any long-lived service that opens descriptors per request needs to know that.

## Candidate calls

| Idea | Why it is warranted, tracing to a specific learning above | Call |
|---|---|---|
| Name all three launchd jobs and the health-check command in "How to run and verify" | The health check became a launchd job on 2026-10-20; the brief's run instructions now leave a user without the job that proves the other two work | Adopt now |
| Replace criterion 10's "after a fresh login with no terminal open" with the observable (job loaded under launchd with a PID, plist sets `RunAtLoad`, health endpoint answers) | Every other criterion is checkable by running something; this one asks for a state only a reboot produces, and the review could only record it as unprobeable | Adopt now |
| Criterion 6's tooling list: name `refresh.py` and all four backfill CLIs | The backfill CLIs are how every added column gets repopulated — learned when `event_type` and `avg_cadence` each needed one — and the brief still names two of four | Adopt now |
| A criterion for the operational layer: the service checks itself and the store is recoverable | Three capabilities exist and were each decided, but a brief that does not mention them makes the next status review score them as scope drift | Adopt now |
| State the activity-day invariant in the metric spec | The store's canonical day is `activity_date` only by accident — the epoch is not persisted — and persisting it naively would move a day-dropping gap into production | Adopt now |
| Reword criteria 2 and 7 so the Garmin Connect comparison is a stated manual step | The comparison is the owner's own acceptance standard and only a human can perform it, but a clause no agent can execute is a clause that silently stops being checked | Park in Later |
| Persist the activity's start epoch as a column | The invariant is now written down, so the decision is recorded; storing the epoch would have to change the window rule with it | Park in Later |
| Faster dashboard-death detection (the health verdict is at most a day old) | The daily check already catches a dead dashboard, so the delay is an implementation choice rather than a target | Park in Later |
| The shared chart template against unusual figure sizes | The degenerate attack covered the values, not the sizes — and a tall figure is what broke the legend once already | Park in Later |
| Re-open what the comparison group should be | Already declined on 2026-10-20 with a reason: the 80/20 plan code is the complete, correct group. The reason still stands | Decline |

## Amendments made to the brief

- **Objective:** unchanged.
- **Criteria added:** "The service checks itself and the store is recoverable — verify by: `.venv/bin/python health_check.py` exits 0 with every check passing, the store's `PRAGMA user_version` equals `store.SCHEMA_VERSION`, and `data/backups/` holds a snapshot written before the last scheduled refresh".
- **Criteria removed or replaced:** none removed. Criterion 6's tooling list and criterion 10's verification method were reworded in place — the first because it named two of four backfill CLIs and no refresh job, the second because it asked for a state that cannot be observed without a reboot.
- **Is-done bar:** unchanged.
- **Out of scope added:** none — the comparison-grouping question was already declined on 2026-10-20 and its reason still stands.
- **Metric spec (not the brief, but the brief delegates the metric definitions to it):** `docs/v2/metrics-spec.md` now states the canonical day (§2.4) and the travel caveat (§7.14). No criterion change was needed: criterion 5 already requires the spec to match the code.

## Later

- Reword criteria 2 and 7 so the Garmin Connect UI comparison reads as a manual spot-check rather than the verification method.
- Persisting the activity's start epoch — with the window rule changed alongside it, never alone.
- Faster dashboard-death detection than the daily health check.
- The shared chart template against unusual figure sizes (tall, wide, single-point).
