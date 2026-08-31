# v2 Metrics Specification

## 1. Decisions (from docs/research/running-metrics-brief.md)
- Default to TREND over ABSOLUTE VALUE everywhere.
- Tier metrics by evidential strength (see §2.1–2.3).
- Recompute what we can reproduce (TRIMP, ACWR, CTL/ATL/TSB, volume, elevation,
  decoupling); ingest reference values with surfaced error bars (VO2max, LT HR,
  race predictions, Garmin load/TE).

## 2. Metric inventory
### 2.1 Primary metrics
- volume.distance_{total,running,treadmill,cross}: weekly km (ISO week),
  rolling4wk, wow_pct.
- load.banister / load.edwards: daily TRIMP.
- pmc.ctl / pmc.atl / pmc.tsb: EWMA tau 42/7.
- load.acwr + load.acwr_pct: coupled 7/28 calendar-day ratio + 180d history percentile.
- fitness.vo2max: ingested per-run Firstbeat estimate.
- lt_hr / lt_pace: ingested, HR anchored, pace flagged. cs_approx: fastest-mile.
  (Stored under load.lt_hr / load.lt_pace / load.cs_approx.)
- race_5k / race_10k / race_half / race_full: ingested, marathon flagged.
### 2.2 Context-only
- elevation.daily/rolling28d/gain_per_km (running) — route context, not a risk metric.
### 2.3 Conditional (aggregated)
- decoupling: eligible runs (sport=running, elapsed>=5400s, gain<=25 m/km,
  route-matched), per-half HR/pace, aggregated >=6 sessions, trend only.

## 3. Formulas
- Banister TRIMP = dur_min * dHR * 0.64 * exp(b * dHR); dHR = (avgHR - HRrest)/
  (HRmax - HRrest), clamped [0,1]; b = 1.92 (M) / 1.67 (F).
- Edwards TRIMP = sum(zone_minutes_i * w_i), w = {1..5}.
- CTL = EWMA(TRIMP, alpha=1/42); ATL = EWMA(TRIMP, alpha=1/7); TSB = CTL - ATL.
- ACWR = mean(daily load, last acute calendar days) / mean(daily load, last chronic
  calendar days), coupled (chronic includes acute days); defaults acute=7, chronic=28.
  Average-normalized so a steady constant load reads 1.0.
- Decoupling = (HR/pace)_2ndHalf / (HR/pace)_1stHalf - 1.

## 4. Parameters (config; defaults in RunnerProfile)
- HRmax, HRrest, sex, birthYear, lthr_manual, hr_zones; Edwards weights {1..5};
  decoupling min-duration 5400 s; gradient 25 m/km; min sessions 6;
  ACWR windows 7/28; PMC tau 42/7; history window 180.

## 5. Context flags carried on every measurement
- hrmax_source (configured | age_predicted), sensor proxy (deviceId),
  route key, indoor/outdoor, gradients, activity-level flags
  (hasIntensityIntervals), and per-metric limitation tags from the field registry.

## 6. Excluded / documented-as-not-metrics
- ACWR banded as injury prediction; efficiency factor as a headline;
  elevation as injury risk; "10% rule"; Garmin proprietary load/TSS as truth.

## 7. Known limitations summary
1. No cycling power in the activity list — cross-training load is HR/time/calories only.
2. Elevation absent on indoor/treadmill/zero-distance-cycling activities.
3. VO2max is a Firstbeat estimate (5–10% error; HRmax-sensitive; underestimates ≥60 mL/kg/min); never recomputed.
4. Anchor on LT heart rate (≈7% error); LT pace can overestimate 20–26%.
5. Race predictors: near-distance (5K/10K/half) safest; marathon least trustworthy.
6. HR zone buckets are device-config-dependent — always store `hr_zones` with the series.
7. Optical vs chest-strap sensor is not reliably distinguishable from the list payload (deviceId is a proxy only).
8. Garmin Training Status / load / TE are proprietary — reference only, never gates.
9. Decoupling is only honest when aggregated over ≥6 sessions on similar flat routes; single-run values are noise (preprint: 57–83% session-residual variance).
10. Banister exponent is population-generic, not individualized (iTRIMP needs lab lactate testing to personalize).
11. Garmin TRIMP is a black box (proprietary beat-to-beat calculation) — we recompute our own TRIMP from the HR series for reproducible units.

## 8. Registry metric keys

```
volume, elevation, trimp_edwards, trimp_banister, ctl, atl,
tsb, acwr, decoupling, vo2max, lt_hr, lt_pace, cs_approx,
race_5k, race_10k, race_half, race_full, load_reference,
cross_training
```

Canonical metric keys (source of truth: `v2/garmin_fields.py` `METRICS`; do not
change here without changing the registry). Generated during Step 3 verification,
kept verbatim so the appendix can be machine-checked.