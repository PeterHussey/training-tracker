# v2 Metrics Specification

## 1. Decisions (from docs/research/running-metrics-brief.md)
- Default to TREND over ABSOLUTE VALUE everywhere.
- Tier metrics by evidential strength (see §2.1–2.3).
- Recompute what we can reproduce (TRIMP, ACWR, CTL/ATL/TSB, volume, elevation,
  decoupling); ingest reference values with surfaced error bars (VO2max, LT HR,
  race predictions). Garmin load/TE ingestion is planned (registry `load_reference`)
  but NOT yet emitted as a metric series — TE values persist as per-activity
  columns only.
- Sport scope: `load.*`, `pmc.*`, `load.acwr*` are computed on **outdoor-running
  activities only**; `treadmill` is its own volume group. Cross-training (bike,
  elliptical, strength...) has no distance, so its load is measured from **HR
  only** and emitted as its own series (`load.banister_cross` /
  `load.edwards_cross`) — it never feeds the running-anchored `pmc.*`/`load.acwr*`
  windows, so a run + bike on the same day is not double-counted. All other sports
  still feed cross-training **volume only** for weekly distance.

## 2. Metric inventory
### 2.1 Primary metrics
- volume.distance_{total,running,treadmill,cross}: weekly km (ISO week),
  rolling4wk, wow_pct.
- load.banister / load.edwards: daily TRIMP, outdoor-running only.
- load.banister_cross / load.edwards_cross: daily TRIMP from cross-training HR
  (background cross load, never merged into running PMC/ACWR).
- pmc.ctl / pmc.atl / pmc.tsb: EWMA tau 42/7, outdoor-running only.
- load.acwr: coupled 7/28 calendar-day ratio, outdoor-running only.
- fitness.vo2max: ingested per-run Firstbeat estimate.
- lt_hr / lt_pace: ingested, HR anchored, pace flagged. cs_approx: fastest-mile.
  (Stored under load.lt_hr / load.lt_pace / load.cs_approx.)
- race_5k / race_10k / race_half / race_full: ingested, marathon flagged.
- age_graded.pct: daily best age-graded performance (WMA 2023) — the day's best
  eligible outdoor run as a fraction of the open-class world record for that age
  and distance (1.0 = the open-class record). Outdoor running only, runs of
  3 km–marathon. Stored under `age_graded.pct`.
### 2.2 Context-only
- elevation.daily/rolling28d/gain_per_km (running) — route context, not a risk metric.
- gap.running: grade-adjusted pace (m/s), outdoor running only. Compensates
  for elevation gain/loss using a Minetti-inspired energy-cost model (K=0.7).
### 2.3 Conditional (aggregated)
- decoupling: eligible runs (sport=running, elapsed>=5400s, gain<=25 m/km,
  route-matched), per-half HR/pace, aggregated >=6 sessions, trend only.
  Emitted by `run_pipeline` as `load.decoupling` per eligible run; the
  aggregated trend needs the session grouping the dashboard builds.

### 2.4 Runalyze VDOT families (ported 2026-10-17)
Faithful port of Runalyze 4.3.x's legacy VO2max chain (AGPL; formulas by
Daniels/Gilbert; branch `support/4.3.x`, source hash
`5669237a78c7d3512df969052fd0d97e5dca6820`). Verbatim pseudocode, provenance map
and per-module attribution live in
`docs/superpowers/specs/2026-10-17-runalyze-vdot-port-design.md`. Registry keys
`vdot_shape`, `marathon_shape`, `prognosis` (see §8); stored series keys:
- `vdot.shape`: daily effective VO2max [mL/kg/min] — duration-weighted mean of
  the per-run pace+HR estimates over the trailing 30-day window, × the race
  corrector (best 3 races by race VDOT; 1.0 when no race qualifies).
- `marathon.shape_pct`: marathon preparedness [%] — 0.67 × weekly-km term +
  0.33 × long-jog term against VO2max-derived targets; 100 = marathon-ready,
  >100 possible.
- `prognosis.<dist>` (dist ∈ 1, 3, 5, 10, 21.1, 42.2, 50 km) and its damped twin
  `prognosis.<dist>_damped`: race time [s] from bisection-inverting the
  Daniels–Gilbert race estimate against the day's shape; the damped twin
  multiplies the shape by the marathon-shape factor before the inversion.

Formulas (bullet summary; the design spec carries the pseudocode verbatim).
Estimator modules: `metrics/effective_vo2max.py` (the pace+HR effective-VO2max
estimate and the race corrector) and `metrics/daniels_gilbert.py` (the
Daniels–Gilbert estimate and its velocity inverse). They are the derivation path
underneath every family in this section, not stored families of their own — they
emit no series and store no rows on their own.
- Effective VO2max (pace + HR): `DG.estimate(v / exp((hr_pct - 1.00466) / 0.68725))`
  with `v = 60000 * km / s` [m/min] and `hr_pct = avgHR / HRmax`.
  The pipeline passes `use_elevation=True` (mirroring the Runalyze account's
  "Adapt for elevation" setting): the pace is computed from the
  elevation-corrected distance, `+2 m` per metre climbed, `-1 m` per metre
  descended (`DistanceModifier` defaults). When the store has no descent
  (`ele_loss_m` is None), the ascent value stands in for both terms — Runalyze's
  own no-route fallback, where a single elevation value feeds both up and down.
  Runs on rolling terrain gain roughly 0.5–0.8% in value, including runs that
  net out: ascent ≈ descent still leaves `+1 m` per metre, since the descent
  term only cancels half the ascent term. Only activities with no elevation
  data at all are unchanged.
- Daniels–Gilbert: `estimate(v) = max(0, -4.6 + 0.182253*v + 0.000104*v^2)`. The
  `v ∈ [50, 1000]` m/min guard is **not** in this function: it lives in
  `estimate_from_race_result` and in the qualifying filter below. A race estimate
  divides by the drop-dead factor
  `0.8 + 0.1894393*exp(-0.012778*t) + 0.2989558*exp(-0.1932605*t)`.
- Velocity inverse: `v = -876 + sqrt(876^2 + (4.6 + vdot) / 0.000104)` m/min.
- Corrector: `max(race_vdot_by_time / race_training_vdot)` over the best 3 races
  by race VDOT; `1.0` only when no race yields a positive ratio (the raw max is
  kept — it is never floored at 1.0).
- Shape: `round(Σ(vdot_i * s_i) / Σ(s_i), 5)` over `[t-30d, t]` (both ends
  inclusive), the per-run `vdot_i` already carrying the corrector.
- Marathon shape: `round(100 * (0.67 * week_km*7/window/target_week_km
  + 0.33 * lj_points*7/70))`, where the weekly-km term is divided by
  `target_week_km = pow(max(vdot, 25.0), 1.135)` and
  `window = min(182, max(70, days_since_first))`; long jogs are runs over 13 km
  within 70 days, linearly down-weighted with age.
- Prognosis: bisect `s ∈ [2*60*dist, 10*60*dist]` for
  `estimate_from_race(dist, s) == vdot`; damped vdot =
  `vdot * min(1, 0.6 + 0.4*max(0, 1 - (dist^1.23 - shape_pct)/100))`.

Qualifying filter (shared by all five computations): sport `running`/`treadmill`,
distance > 0, moving time > 0, average HR present and ≤ profile HRmax, and raw
Daniels–Gilbert velocity within `[50, 1000]` m/min; with no resolvable HRmax
(`hrmax is None`) nothing qualifies. This is the port's stateless
stand-in for Runalyze's manual `use_vo2max` curation, so two consequences follow:
runs without usable HR are excluded and `vdot.shape`/`marathon.shape_pct` can read
lower than a Runalyze instance that counts them; and the marathon-shape targets
always resolve from the **current** corrected shape even for a historical as-of
day (Runalyze-faithful, not self-consistent history), while the 182-day week-km
window's minimum-clamp input is the earliest activity date in the store (Runalyze
keys it on the account registration date).

Race anchoring: a race is an activity tagged in **Garmin Connect** with event type
`Race`, carried through as `eventType.typeKey == "race"` (`normalize.py` →
`Activity.event_type`; see `docs/v2/garmin-field-mapping.md`). Tag it **before the
first refresh that ingests the activity** — the tag arrives with that first
ingestion. A tag added to an activity this dashboard has already stored is **not**
picked up: neither refresh mode revisits stored activities (incremental starts at
`latest_activity_date + 1 day`; historical fetches backwards from the earliest
stored date). With no tagged race the corrector stays at 1.0 — a graceful dormant
state, never a filler factor.

A tag added after ingestion is repaired with the script, not a refresh: run
`.venv/bin/python backfill_event_types.py` (dry run by default) to see which
stored rows would change, then rerun with `--apply` to write. It re-reads the
**live** activity-list payload for the store's date range — never the cached
`activity_summary_{id}.json` files, which predate the tagging — and narrow-
UPDATEs only the `event_type` column, so backfilled RPE/Feel/weather survive.

Null behavior (never a filler value): the `vdot.shape` series function emits `0.0`
for a day whose 30-day window is empty, but the **pipeline** drops those days from
the stored rows, so a day whose window holds no qualifying activity stores no row
at all — a gap longer than the window stays a gap. The one surviving zero is a
window holding only activities whose estimate is 0 (e.g. `avg_hr == 0`): the day
counts as non-empty and stores a real `0.0`. `prognosis.*` rows are absent where
the bisection has no valid answer — the raw row when the raw vdot is outside
`[15, 90]`, a damped row when the **damped** vdot is. The prognosis table and the
paces table are hidden when their inputs are absent.

Canonical day: an activity's day is its `activity_date` — the calendar date of
Garmin's local start time (`startTimeLocal`). The shape window `[t-30d, t]` is
evaluated on that day: the series is keyed by `activity_date`, and the window
filter matches activities by start instant (the `beginTimestamp` epoch, or noon
of the stored day when no epoch is available) against the day's window ending
at 23:59:59 local. The store does not persist the epoch — a stored activity
reloads with no timestamp and falls back to noon of its own day — so for stored
data the day and the window always agree. See §7.14 for what that costs after a
timezone change.

Training paces: a display-only table at 65 → 110 % of vVO2max in 5 % steps, from
the current shape rounded half-up via `daniels_gilbert.pace_s_per_km`. It is
derived in-session (bottom of Overview) and is **never stored**.

## 3. Formulas
- Banister TRIMP = dur_min * dHR * 0.64 * exp(b * dHR); dHR = (avgHR - HRrest)/
  (HRmax - HRrest), clamped [0,1]; b = 1.92 (M) / 1.67 (F).
- Edwards TRIMP = sum(zone_minutes_i * w_i), w = {1..5}.
- CTL = EWMA(TRIMP, alpha=1/42); ATL = EWMA(TRIMP, alpha=1/7); TSB = CTL - ATL.
- ACWR = mean(daily load, last acute calendar days) / mean(daily load, last chronic
  calendar days), coupled (chronic includes acute days); defaults acute=7, chronic=28.
  Average-normalized so a steady constant load reads 1.0.
- Grade-Adjusted Pace (GAP) = actual speed × (1 + k × grade), where
  grade = ele_gain_m / distance_m, k = 0.7 (Minetti-inspired; literature range 0.6–1.0).
- Decoupling = (HR/pace)_2ndHalf / (HR/pace)_1stHalf - 1.
- Age grading (WMA 2023): `graded_time = time_s * F(age, event)` where `F` is the
  WMA 2023 age factor for the age and event (table lookup, age clamped to 30–110);
  `graded_pct = WR_open(event) / graded_time`, so 1.0 = the open-class world record
  at that age. Both the open record and the age factor are interpolated log-log
  between the bracketing event anchors (3k, 5k, 10k, half, marathon) when the
  distance is not one of them. The daily series keeps the best (max) graded pct
  of that day.

## 4. Parameters (config; defaults in RunnerProfile)
- HRmax, HRrest, sex, birthYear, lthr_manual, hr_zones; Edwards weights {1..5};
  decoupling min-duration 5400 s; gradient 25 m/km; min sessions 6;
  ACWR windows 7/28; PMC tau 42/7.
- HRmax sources: `configured` (manual), `age_predicted` (default), `observed`
  (recurring max HR on >=2 distinct days over past workouts; one-off spikes are
  discounted as artifacts). `observed`/`configured` never downgrade to age
  prediction; configuration always wins over estimation.

## 5. Context flags carried on every measurement
- Actually carried today: `hrmax_source` (configured | age_predicted | observed)
  on `load.*`; `basis` (running | treadmill | cross_training) on load series
  scoped by activity type; `error_class`/`recompute` on fitness.vo2max; `anchored`/
  `error_class` on lt_hr/lt_pace; `error_class` on race_*; per-metric limitation
  tags come from the field registry. Registered-but-NOT-yet-carried: sensor proxy
  (deviceId), route key, indoor/outdoor, gradients, hasIntensityIntervals.

## 6. Excluded / documented-as-not-metrics
- ACWR banded as injury prediction; efficiency factor as a headline;
  elevation as injury risk; "10% rule"; Garmin proprietary load/TSS as truth.

## 7. Known limitations summary
1. Cross-training load is HR/time/calories only (no cycling power in the activity
   list); `load.edwards_cross` can be 0 for sessions without zone seconds.
2. Estimated HRmax uses the recurring observed max, which can lag a genuine
   high-intensity max effort (discounted as a one-off spike) — for those, set
   `--hrmax` manually. HRmax-sensitivity: a higher HRmax lowers every TRIMP.
3. Elevation absent on indoor/treadmill/zero-distance-cycling activities.
4. VO2max is a Firstbeat estimate (5–10% error; HRmax-sensitive; underestimates ≥60 mL/kg/min); never recomputed.
5. Anchor on LT heart rate (≈7% error); LT pace can overestimate 20–26%.
6. Race predictors: near-distance (5K/10K/half) safest; marathon least trustworthy.
7. HR zone buckets are device-config-dependent — always store `hr_zones` with the series.
8. Optical vs chest-strap sensor is not reliably distinguishable from the list payload (deviceId is a proxy only).
9. Garmin Training Status / load / TE are proprietary — reference only, never gates.
10. Decoupling is only honest when aggregated over ≥6 sessions on similar flat routes; single-run values are noise (preprint: 57–83% session-residual variance).
11. Banister exponent is population-generic, not individualized (iTRIMP needs lab lactate testing to personalize).
12. Garmin TRIMP is a black box (proprietary beat-to-beat calculation) — we recompute our own TRIMP from the HR series for reproducible units.
13. Age grading uses the WMA 2023 age-factor table and the ratified **open-class**
    world records (as of 2026): it compares you to the open class, so 1.0 means
    "world-record pace for your age", not a fitness score, and it is not a
    prediction of future performance. A run outside 3 km–marathon stores no row
    at all (no zero filler); an age outside 30–110 is clamped to the table's
    edge rather than left ungraded. The age used is `activity year - birth year`.
14. The activity's day is Garmin's local start date, but its start *instant* is
    the epoch Garmin reports, and the two can disagree after a timezone change
    or travel. Stored activities carry only the day (no epoch is persisted), so
    the shape window is self-consistent for everything the store holds; an
    activity parsed straight from a live payload keeps its epoch, and if that
    instant falls on a different calendar day than `startTimeLocal`, its own day
    may hold no qualifying run in the trailing 30-day window — that day is then
    dropped from `vdot.shape` (a gap, never a zero). Persisting the epoch
    without changing the window rule would move this gap into production.

## 8. Registry metric keys

Every `metric_series` row carries a key, and the pipeline's complete key set is
listed below — one key per line, sorted. `health_check.py`'s `spec-covers-keys`
probe fails when the store holds a key this list omits, so a new metric has to
be registered here in the same change that emits it.

```
age_graded.pct
elevation.daily_gain_running
elevation.gain_per_km_running
elevation.rolling28d_running
fitness.vo2max
gap.running
injury.max_run_ratio
load.acwr
load.acwr_pct
load.banister
load.banister_cross
load.banister_running
load.banister_treadmill
load.cs_approx
load.decoupling
load.decoupling_mean
load.edwards
load.edwards_cross
load.edwards_running
load.edwards_treadmill
load.lt_effort_dots20_hr
load.lt_effort_dots20_pace
load.lt_effort_dots30_hr
load.lt_effort_dots30_pace
load.lt_hr
load.lt_hr_best20
load.lt_hr_best30
load.lt_pace
load.lt_pace_best20
load.lt_pace_best30
marathon.shape_pct
pmc.atl
pmc.ctl
pmc.tsb
prognosis.1
prognosis.10
prognosis.10_damped
prognosis.1_damped
prognosis.21.1
prognosis.21.1_damped
prognosis.3
prognosis.3_damped
prognosis.42.2
prognosis.42.2_damped
prognosis.5
prognosis.50
prognosis.50_damped
prognosis.5_damped
race_10k
race_5k
race_full
race_half
vdot.shape
volume.distance_cross
volume.distance_running
volume.distance_running_daily
volume.distance_strength
volume.distance_total
volume.distance_total_daily
volume.distance_treadmill
volume.distance_treadmill_daily
volume.duration_cross
volume.duration_cross_daily
volume.duration_running
volume.duration_running_daily
volume.duration_strength
volume.duration_strength_daily
volume.duration_total
volume.duration_total_daily
volume.duration_treadmill
volume.duration_treadmill_daily
```

Naming rules: the first segment is the family (`volume`, `load`, `pmc`, …);
`_daily` marks the daily series a windowed aggregate is built from; `_running`,
`_cross`, `_strength`, `_treadmill` mark a sport split; `prognosis.<distance>`
is a race-distance prognosis in km and `_damped` applies the marathon-shape
damping factor. Keys absent from a store are conditional on the data
(`session.CONDITIONAL_METRICS` records the reason for each).

The Garmin field mapping speaks a different vocabulary on purpose:
`garmin_fields.py` `METRICS` names metric *families* with the field-mapping
labels (`trimp_edwards`, `ctl`, `marathon_shape`, …) used by
`docs/v2/garmin-field-mapping.md`, not the stored keys above. It is not the
source of truth for this list.