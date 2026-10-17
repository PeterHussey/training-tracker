# Design: Runalyze VDOT & Paces port for training-tracker

**Date:** 2026-10-17
**Status:** draft → builder check → approval pending
**Provenance:** brainstorming session 2026-10-17; Runalyze source verified by reading (`Runalyze` branch `support/4.3.x`, shallow clone at `/private/var/folders/02/r2yk1_2j15b306873bm6gdt40000gp/T/runalyze-src` — re-clone if missing).

## Intent & why

The owner runs a personal Streamlit dashboard on Garmin data (this repo) and keeps **Runalyze** (self-hosted, AGPL, open source) open side-by-side for three lookups: an independent VO2max estimate, a race-prognosis table, and training paces. This port makes the tracker answer those three directly so the side-by-side lookup goes away.

Bookmark from the conversation (user ground rules):

> "Don't reinvent anything; only use the open-source repo."

Port Runalyze's actual algorithms verbatim (PHP → Python), with attribution. No homegrown approximations, no formula "improvements", no extrapolations. Anything Runalyze computes as null/invalid stays null/invalid here.

## Goals

1. **Training-based effective VO2max, per activity** — Runalyze's DanielsGilbert pace+HR estimate (`LegacyEffectiveVO2max::fromPaceAndHR`), computed for qualifying running activities during the pipeline.
2. **Shape time series** — daily value = duration-weighted mean of per-activity VO2max over a rolling **30-day** window (`JD\Shape`), × corrector factor (race anchoring; 1.0 with no races). Metric key: `vdot.shape`.
3. **Race prognosis table** — distances **1, 3, 5, 10, 21.1 (HM), 42.2 (M), 50 km**; per distance: seconds from bisection-inverting the DanielsGilbert race estimate against the corrected shape (`Prognosis\VO2max::getSeconds`), both **raw** and **marathon-shape damped** variants (Runalyze's own prognosis panel plots both).
4. **Marathon shape %** (`BasicEndurance` / `MarathonShape`) — 0.67 × weekly-km fit + 0.33 × long-jog quality vs VO2max-derived targets. Metric key: `marathon.shape_pct`.
5. **Training paces table** — pace at 65→110% of vVO2max in 5% steps, from the **current rounded shape** via `VO2maxVelocity::paceAt` (Runalyze's tools page "tableVo2maxPace"). Display-only, derived in-session (not stored).

## Non-goals

- 80/20 (Fitzgerald) zones — explicitly declined by the user.
- Runalyze UI parity: activities browser, gear, week summaries, training plans, tools pages other than the three surfaces above.
- Alternative prognosis models (Bock, Cameron, Steffny) — not ported.
- Race-tag UI or a race-results CRUD — race anchoring rides on **Garmin event-type tags**, zero new UI (user decision, see "Decisions").
- Elevation correction UI — constant shipped, default OFF (Runalyze's own default).
- New Garmin endpoints — everything derives from already-fetched activity data, LT payload, and the profile.

## Anti-goals (what would make this a failure)

- Invented or approximated constants/formulas; plausible filler when the math says "unknown". The repo's "is done" bar is: valid Garmin-derived values or nothing.
- A homegrown pace-zone scheme replacing the ported percentage table.

## Source of truth: Runalyze math, ported verbatim

All names below are Runalyze 4.3 paths relative to `inc/core/`.

### 1. DanielsGilbertFormula (`Sports/Running/VO2max/Estimation/DanielsGilbertFormula.php`)

```python
def estimate_from_velocity(v_kmh_... ):  # v in m/min, returns 0 outside [50, 1000]
vo2max_from_velocity = -4.6 + 0.182253*v + 0.000104*v**2
drop_dead(t_min) = 0.8 + 0.1894393*exp(-0.012778*t_min) + 0.2989558*exp(-0.1932605*t_min)
estimate_from_race(km, s) = estimate(velocity) / drop_dead(s/60)   # v = 1000*km/(s/60)
```

### 2. VO2maxVelocity (`Sports/Running/VO2max/VO2maxVelocity.php`) — inverse

```
v = -876 + sqrt(876**2 + (4.6 + vo2max)/0.000104)   # m/min
pace_s_per_km = round(60000 / v)                    # pace(vdot) and paceAt(vdot, pct) (pct multiplies vdot)
```

### 3. Per-activity effective VO2max (`Calculation/JD/LegacyEffectiveVO2max.php`) — deprecated upstream but still active in 4.3

```
hr_in_percent = activity_avg_hr / profile.hrmax          # fraction, e.g. 0.85
percentage_at_hr(h) = exp((h - 1.00466) / 0.68725)       # inverse: hr_at(p) = 0.68725*ln(p) + 1.00466
real_speed = 60*1000*km/s                                 # m/min
value = estimate(real_speed / percentage_at_hr)           # before corrector
exact_value = corrector.factor() * value
```

### 4. Corrector (`Calculation/JD/LegacyEffectiveVO2maxCorrector.php`)

`factor() = max(1, race_vdot_by_time / race_training_vdot)` over the **best 3 races** (ordered by race VDOT desc), default **1.0** when no races. In this port, non-circular restatement (marked my call): for each event-type-tagged race activity with distance/time, `race_vdot = daniels_gilbert(dist, time)`, `training_vdot = LegacyEffectiveVO2max.fromPaceAndHR(...)` without corrector; factor = max of the three ratios, with the documented default 1.0 when no races exist (Runalyze keeps the raw max as-is).

Optional manual corrector (Runalyze: numeric 0.5–2.0) is ported as an optional parameter, **no UI**.

### 5. Shape (`Calculation/JD/Shape.php`)

```
shape_day(t) = Σ(vo2max_i × s_i × use_i) / Σ(s_i × use_i)   over [t - 30 days, t]  # VO2MAX_DAYS=30; window is `BETWEEN t-30d AND t` inclusive of day t
corrected = corrector.factor() × shape_day
```
Elevation-corrected variant exists upstream (`+2%/100 m` up, `−1%/100 m` down, config default **OFF**) — port the constants as an optional param, never default-on, no UI.

### 6. Marathon shape (`Calculation/BasicEndurance.php`, thin wrapper `Sports/Running/MarathonShape.php`)

```
target_week_km    = pow(max(vdot, 25.0), 1.135)
target_lj_km_week = ln(max(vdot, 25.0)/4) * 12   # the 13 km minimum is ALREADY subtracted in the source's target used for normalization
long-jog window: DAYS_FOR_LONGJOGS = 70 days; min long jog = 13 km
weekly-km window: DAYS_FOR_WEEK_KM = 182 days (min 70 for young histories)
points_i = (2 - (2/70)*floor(daysAgo_i)) * ( (dist_i - 13)**2 )
longjog_percentage = points_total / (target_lj_km_week/7) / days...   # pinned by BasicEnduranceTest numbers below
shape_pct = round(100 * (0.67*week_km/target_week_km + 0.33*longjog_pct_normalized))
# 100 = ideal marathon preparation; >100 possible (ultra territory)
```
The exact normalization of the long-jog term and the reduced long-jog target are pinned by Runalyze's own test numbers (BasicEnduranceTest.php in the 4.3 tests tree) — do not derive them independently. One semantic to port faithfully (marked my call): `BasicEndurance::asArray` resolves the knock-on targets from the **current** effective VO2max even when evaluated at a historical timestamp — our `marathon.shape_pct` history is therefore not self-consistent across days; it is Runalyze-faithful. Runalyze keys the 70-day-minimum week-km clamp on account registration date; this repo's equivalent is the earliest activity date in the store (marked my call).

### 7. Race prognosis (`Sports/Running/Prognosis/VO2max.php`, `Mathematics/Numerics/Bisection.php`)

```
valid vdot ∈ [15, 90]                      # else null
search s ∈ [2*60*dist, 10*60*dist]         # bisection; target: estimate_from_race(dist, s) == adjusted_vdot
marathon-shape damping (flag): factor = min(1.0, 0.6 + 0.4*max(0.0, 1 - (dist**1.23 - shape_pct)/100.0))
damped variant: factor × effective_vo2max FIRST, then bisection on the adjusted target  # getAdjustedVO2maxForDistance
raw variant: flag off → factor 1, unchanged
```
(The factor divides nothing: it multiplies the target VO2max *before* the bisection, not the resulting seconds — `Prognosis\VO2max::getSeconds`/`getAdjustmentFactor`, lines 91–165 in the 4.3 source.)

## Mapping onto this repo

- **Modules** (repo convention: one concern per module, pure, no DB/network): `metrics/daniels_gilbert.py` (formulas 1–2), `metrics/effective_vo2max.py` (formula 3), `metrics/vdot_shape.py` (formula 5), `metrics/prognosis.py` (formula 7), `metrics/marathon_shape.py` (formula 6), `metrics/vdot_paces.py` (paces table). Exact naming is the builder's.
- **Railway**: produced inside `pipeline.compute_metric_rows` alongside existing families; stored via `INSERT OR REPLACE INTO metric_series` (existing flow). Dotscored keys: `vdot.shape`, `prognosis.<dist>`, `marathon.shape_pct`; paces table is display-time only.
- **Qualifying activities** (marked my call): sport `running` or `treadmill_running`, distance > 0, moving time > 0, avg HR present and ≤ profile HRmax, and DanielsGilbert velocity guard in-bounds; else excluded from all five computations. This mirrors Runalyze's running-only semantics while respecting the repo's sport mapping. Cross/strength excluded (duration-only loads, per AGENTS.md).
- **Race anchoring**: user tags the race activity in **Garmin Connect** (event-type "Race") — no tracker UI. `normalize.py` currently reads only `activityType.typeKey` and must be extended to carry the Garmin event-type field from the 0.3.2 payload (field name to be verified during build; worst case the corrector stays dormant at 1.0). Propagation: tagged-after-the-fact edits are picked up by the sidebar's **historical** fetch mode (`save_activities` is `INSERT OR REPLACE`; metrics persist recomputes over the full activity set). The regular incremental refresh does **not** re-fetch past activities — this behavior is intentional here, matches the repo's refresh design, and the race-day flow is: tag in Garmin → next dashboard session, run historical fetch once.
- **Surfaces (user decision: layout "B", distributed):** shape trend beside/overlaid on the existing Garmin VO2max trend chart; prognosis table below the existing Garmin race-predictions chart; paces table appended at the bottom of Overview; marathon-shape detail beside the prognosis table. Activity view unchanged (no toggle).
- **Test fidelity anchor:** the 4.3 tests tree pins the numeric expectations but is MySQL-dependent (`@group dependsOnOldDatabase`, legacy `DB::` facade) — port *assertion numbers*, not runnable code. Existing: `tests/inc/core/Calculation/BasicEnduranceTest.php`, `tests/inc/core/Calculation/JD/ShapeTest.php`, `tests/inc/core/Calculation/JD/LegacyEffectiveVO2maxTest.php`, `tests/inc/core/Mathematics/Numerics/BisectionTest.php`, `tests/inc/core/Sports/Running/MarathonShapeTest.php`. There are **no** upstream tests for the DanielsGilbertFormula or Prognosis\VO2max classes — for those, goldens are generated by evaluating the PHP directly against the clone (builder's method; `php`-CLI or transcription, my call). Our new tests must reproduce those numbers before any surface polish.
- **Attribution**: module docstrings credit "Runalyze (AGPL), ported from inc/core/…, formulas by Daniels/Gilbert"; a note lands in docs/v2/metrics-spec.md.

## Constraints

- Garmin-derivable only (PROJECT.md brief); personal use; Python 3.11+; ruff line-length 100; pytest per repo command set; no new dependencies.
- Reproduce Runalyze defaults wholesale: 30-day shape window, ±2/1 %/100m elevation correction (off), corrector best-3, 0.67/0.33 marathon-shape weights, 182/70-day windows, 13 km min long jog, 25.0 VO2max floors.
- Where the repo's conventions and Runalyze's semantics can't both hold, Runalyze wins inside the five computations; the repo's conventions win in storage/surface plumbing.
- **This document cites source pseudocode for orientation; where any pseudocode here and the Runalyze PHP disagree, the PHP is the spec** (the reviewer check already caught one such case in §7 and fixed it).

## Decisions (recorded from the conversation)

1. **User — "Don't reinvent anything; only use the open-source repo."** Verbatim port, Runalyze formulas/constants only.
2. **User — 80/20 zones out**; instead "import their training paces calculations" (verbatim Runalyze paces table).
3. **User — layout "B", distributed:** no new tab; Runalyze pieces live beside what they compare against (shape vs Garmin VO2max trend, prognosis under Garmin race predictions, paces at Overview bottom). Mockup reviewed in the visual companion.
4. **User — race anchoring, lightest viable:** "Races are very infrequent" → no race UI in the tracker; user tags races with Garmin's event-type "Race" in Garmin Connect; `normalize.py` picks it up. Confirmed in conversation: user is willing ("I can do that"). An earlier idea of a manual race-entry table was superseded by this decision.
5. **User & conversation — propagation of after-the-fact edits:** accepted that the regular incremental refresh does NOT re-fetch old activities; the fix is the existing sidebar **historical** fetch mode once after tagging (INSERT OR REPLACE + full-set metric persist). Verified against dashboard.py/store.py.
6. **My call — corrector dormancy is an acceptable terminal state:** if Garmin's event-type field doesn't surface through the 0.3.2 payload, the corrector stays 1.0 and the estimates remain purely training-based; that is explicitly a graceful state, not a bug. A user-set non-1.0 corrector in their *Runalyze account* may therefore make numbered outputs diverge from Runalyze's panel until a race is tagged here — by design; not to be papered over with a fake factor.

## Open question for the owner (kept out of the build's critical path)

- **HRmax reconciliation:** Runalyze's per-activity estimates divide avg HR by the **account-configured** HRmax; this repo resolves HRmax dynamically (`hrmax_source`: configured → observed → age-predicted). If the Runalyze account's configured HRmax differs from this repo's resolved value, every VO2max/shape/pace number shifts proportionally. The port uses the repo's resolved HRmax (repo convention wins in plumbing); the owner should check whether their Runalyze account has a custom HRmax set, and align the repo's configured HRmax if they want the two tools to agree to the decimal rather than the math.

## Verification items (build-time)

1. Garmin event-type field passes through garminconnect 0.3.2 into `normalize.py`'s Input objects (field name + payload shape).
2. Historical-fetch propagation of an edited/tagged old activity end-to-end (race corrector updates on the next refresh after historical fetch).
3. Null behavior when a window has zero qualifying activities (paces table hidden; prognosis silently empty, never filler).
