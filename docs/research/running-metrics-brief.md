# Research Brief: Running Metrics Computable from Garmin Data

Prepared as the evidence base for the v2 training-tracker rebuild. Organized by four categories, each metric with **methodology**, **use in training**, and **evidence + limitations**. Sources prioritized: peer-reviewed validation studies, systematic reviews/meta-analyses, and Firstbeat/Garmin white papers. Several claims in a commonly shared starting heuristic list need correction — flagged inline.

---

## 1. Training Load

### 1.1 Acute:Chronic Workload Ratio (ACWR)

**Methodology.** Acute load = sum of load (distance, minutes, or TRIMP) over the last 7 days; chronic load = 4-week rolling average (28 days). Ratio can be computed "coupled" (acute included in chronic) or "uncoupled"; smoothing can be simple rolling average or exponentially weighted (EWMA). Garmin exposes this directly as **Load Ratio** (acute 7-day load vs. 28-day chronic, banded <0.8 / 0.8–1.4 balanced / 1.5–1.9 / ≥2.0) via the Firstbeat engine; Firstbeat Sports uses 0.7–1.4 as "sweet spot."

**Use in training.** Monitors whether load is spiking or collapsing relative to a runner's recent history — a context/warning dashboard, not a prescription.

**Evidence & limitations.** Two systematic reviews + one meta-analysis (Maupin et al. 2020, *Open Access J Sports Med*; Li 2026, registered systematic review; 2025 meta, *BMC Sports Sci Med Rehabil*) find high ACWR is *associated* with injury risk with a trend toward lowest risk in 0.8–1.3 — but all three explicitly warn ACWR should **not** be used as a standalone individual injury-prediction tool or universal threshold (2025 meta's "safe zone" CI was 0.14–0.94, effectively unusable). Critically, the one large prospective *running*-specific cohort (Nakaoka et al. 2021, *Sports Med*, n=435 Dutch recreational runners) found the **opposite**: an L-shaped *inverse* association (ACWR <0.70 → ~10% injury probability; >1.38 → ~1%). The 0.8–1.3 "sweet spot" originates from team-sport literature (Gabbett), not running. EWMA methods were non-significant in that running cohort. **Bottom line:** treat ACWR/Load Ratio as a monitoring signal for rapid load swings, never as a deterministic risk gate; rely on your own individual history bands, not population cutoffs.

### 1.2 TRIMP (Training Impulse)

**Methodology.** Banister's TRIMP = duration × ΔHR ratio × exponential intensity weighting (ΔHR = (HR−HRrest)/(HRmax−HRrest); exponent b ≈ 1.92 men / 1.67 women, from generic HR–lactate curves, applied to *mean* session HR). Variants: Edwards TRIMP (time in 5 arbitrary HR zones × linear weights 1–5), Lucia TRIMP (3 zones anchored to ventilatory thresholds), individualized iTRIMP (exponent fitted to the athlete's own lactate profile). Garmin's "Training Load" is Firstbeat's proprietary modification: Banister-style TRIMP but computed from **beat-to-beat HR** (handles intervals better) with a floor on intensity; Training Effect (aerobic/anaerobic) is derived from an EPOC model.

**Use in training.** Quantify internal load of one session and accumulate across a week for dose–response planning and periodization.

**Evidence & limitations.** Banister and Edwards TRIMP correlate ~nearly perfectly with each other (r=0.89, Haddad 2012) and moderately with external markers, but test–retest reliability of Banister TRIMP is **poor** (CV ≈ 15.6%; raw HR CV ≈ 3.9% — Wallace et al. 2014). Edwards' zone weights are theoretically derived, never validated, and its linear weighting (zone 5 = 5× zone 1) contradicts the exponential physiology behind Banister's model. iTRIMP has the strongest dose–response validity (Manzi 2009) but needs lab lactate testing to personalize. **Bottom line:** TRIMP is useful for *relative* load trends; absolute scores across athletes or versions aren't comparable. Garmin's load number is a black box — recompute your own from HR time series if you want reproducible units.

### 1.3 CTL / ATL / TSB (Performance-Management model)

**Methodology.** Banister's 1975 impulse-response model: performance(t) = p₀ + fitness(t)·k₁ − fatigue(t)·k₂, with two exponential decays (fitness slow, fatigue fast). TrainingPeaks' PMC (Coggan/Allen) drops the gain terms and models fitness/fatigue as exponentially weighted moving averages — CTL (fitness, τ=42d), ATL (fatigue, τ=7d), TSB = CTL − ATL (form). For runners the input is a HR-based stress score (TSS/HRSS/TRIMP) since there's no power meter.

**Use in training.** Periodization and taper planning ("plan season on CTL, week on ATL, race on TSB"); rough target bands (race-day TSB ≈ +5 to +25; productive build ≈ −25 to −10).

**Evidence & limitations.** The Banister IR model has repeatedly predicted training-induced performance changes across sports (TrainingPeaks' own review; Bayesian re-implementations confirm the math). But the PMC version is a **deliberate simplification**: dropping gain factors means TSB indicates "freshness/adaptation," *not* actual performance; the 42/7-day time constants are nominal defaults with no objective tuning guidance; and ATL time-constant choice materially changes TSB. TSB is mathematically near-equivalent to ACWR expressed as a difference — same evidential caveats apply. **Bottom line:** excellent for *visualizing* load/freshness trajectories; over-interpretation of specific band thresholds is a heuristic, not science.

---

## 2. Fitness Trend

### 2.1 VO₂max estimate over time

**Methodology.** Garmin uses Firstbeat's submaximal model relating HR to running speed during any free run; it selects reliable HR/speed segments and requires a HRmax input (age-predicted unless overridden). Published automatically with each run.

**Use in training.** Monthly/yearly trend of aerobic capacity; it's the primary input to Garmin's Training Status and race predictor.

**Evidence & limitations.** Firstbeat's validation (white paper, n=2690 free runs): MAPE ≈ **5%** for running, improves to ~4% if true HRmax is entered; HRmax errors propagate (15 bpm error → 7–9% VO₂max error). Independent studies: Forerunner 245 MAPE 7–8% overall but **fitness-dependent — moderately trained runners ~2.8%, highly trained (>60 mL/kg/min) ~9.4–10.4% with systematic underestimation** (2025, *Eur J Appl Physiol*); Fenix 6 valid in athletic populations (MAPE 6.85%, CCC 0.70; Carrier 2023). A 2025 systematic review (*Front Sports Active Living*) concludes Garmin/Firstbeat VO₂max is valid for healthy, recreational, and team-sport professionals but **questionable for elite endurance athletes**. Chest-strap HR >> optical wrist sensor. **Bottom line:** excellent *trend* metric for recreational-to-good runners; don't treat as a lab measurement; note the underestimation bias as fitness rises.

### 2.2 Critical velocity / lactate-threshold pace

**Methodology.** Critical speed (CS) = the running speed at the maximal metabolic steady state, computed from the speed–duration relationship (two-three maximal efforts ~3–15 min; field alternatives: timed trials or 3-min all-out test). Garmin instead reports Firstbeat's **lactate threshold (LT) pace & HR**, from a guided LT test or newer auto-detection using recent runs + estimated VO₂max.

**Use in training.** Anchors training zones (easy/threshold/interval) and funnels into race prediction; CS itself is a strong determinant of mile–marathon performance.

**Evidence & limitations.** CS is repeatedly validated as the heavy/severe intensity boundary, and CS (not MLSS) best represents true maximal metabolic steady state (Nixon et al. 2021). Two-parameter CS models predict 5-km time well (R² ≈ 0.90; *Front Physiol* 2021); a 2026 scoping review (*Sports Med*) confirms strong performance associations but notes no consensus on protocols. Field TT/3MT tests are reliable under standardized conditions (2025 systematic review). **Garmin LT validity is mixed:** Fenix 6 LT speed MAPE 7.5–8.4% (valid; Carrier), but a 2025 study found Garmin **overestimates LT pace by ~20–26%** MAPE while LT *HR* error stays ~7%; HR@LT agreement is poor (CCC ≈ 0.35). No device met statistical equivalence to lab values. **Bottom line:** CS/LT is the most physiologically grounded fitness-trend metric you can derive from Garmin data — but anchor on *threshold HR* (or manually set from a lab test / race), not the device's auto-reported threshold pace.

### 2.3 Race-time predictor trend

**Methodology.** Most predictors use the Riegel power law, T₂ = T₁ × (D₂/D₁)^1.06 (a fixed "fatigue factor" fitted to world-record/population data); Daniels' VDOT is arithmetically equivalent to a Riegel model with a fixed factor; Garmin's Race Predictor is Firstbeat-derived (model + recent VO₂max estimate).

**Use in training.** Long-range goal-setting and tracking which race distances are improving; training-pace derivation (VDOT).

**Evidence & limitations.** Riegel is well-calibrated **up to half-marathon but dramatically underestimates marathon time** — ≥10 minutes too fast for ~half of runners (Vickers & Vertosick 2016, n≈2300). Training-volume-aware models cut error substantially (MSE 381 → 208) — i.e., a 50:00 10K runner's marathon depends on weekly volume, which Riegel ignores. The fixed 1.06 factor is a bias–variance compromise; fitting the exponent to an individual's own race results is more accurate (power-law modeling). Extrapolation across large distance gaps (5K→marathon) is the weakest signal. **Bottom line:** use near-distance predictions and *trend direction* across months; treat any single marathon prediction as the least trustworthy number in the app.

---

## 3. Efficiency

### 3.1 Aerobic decoupling (HR vs. pace drift)

**Methodology.** Compute average pace and average HR over each half (or km segments) of a sustained-effort run; decoupling = (HR/pace)₂nd-half ÷ (HR/pace)₁st-half − 1, as a percentage. Research-grade versions normalize to %HRmax and %critical speed and report magnitude + "onset distance" (the point where decoupling exceeds ~1.025). TrainingPeaks exposes this as "Pa:HR."

**Use in training.** Quantify "durability" — how well physiology holds up over time; trending it down across months indicates improving aerobic base.

**Evidence & limitations.** Strong emerging support: in 82,303 marathon finishers (Smyth et al. 2022, *Eur J Sport Sci*), mean decoupling ≈ 16%, and adding decoupling magnitude+onset to a CS-based performance model cut prediction error 6.45% → 5.16% (~20% improvement). HR/breathing-rate decoupling predicts loss of the aerobic (VT1) threshold during prolonged exercise (Rothschild et al. 2025, R²≈0.93–0.95). **However,** a preprint analyzing 253,000 workouts found decoupling variance is dominated by **day-to-day noise** (57–83% of HR-response variance was session-residual; route-matched 27–53%), route geometry alone predicts speed-based decoupling (CV R² 0.82), and ~6+ sessions are needed to reach reliable estimates. Heat, hydration, altitude, sleep, and accumulated fatigue all masquerade as decoupling; HR dissociates from true metabolic cost during long running. The TrainingPeaks "<5% good / 5–8% monitor / >8% flag" bands are folklore, not validated thresholds. **Bottom line:** real signal, but only honest when (a) aggregated over ≥6 sessions on a **similar route/conditions**, (b) single-effort durations ≥~90 min, (c) presented as a trend, not a per-run number.

### 3.2 Efficiency factor (pace ÷ HR, or power ÷ HR) & running economy

**Methodology.** Field proxy: pace (or derived power) divided by HR at matched effort, tracked across weeks. True running economy = submaximal O₂ cost at a given speed, measured with a metabolic cart.

**Use in training.** "Same effort, better output" tracking.

**Evidence & limitations.** Running economy is one of the strongest lab predictors of distance performance (for elite runners it predicts better than VO₂max), and its intraindividual reliability is good under controlled lab conditions (typical error 1.3–5%; smallest worthwhile change ≈ 2.4%; Saunders 2004). But the *field* pace/HR ratio is not validated as an economy proxy: it conflates economy with cardiovascular drift, and runner-level signal accounts for only ~22–43% of variance in HR-derived metrics even route-matched. There's no metabolic-cart data in Garmin output, so a true economy figure isn't obtainable from the watch. **Bottom line:** drop or demote "efficiency factor" as a headline metric; it's a noisy derived field index unless paired with decoupling-style aggregation, context controls, and hard zone normalization (relative rather than absolute pace).

---

## 4. Volume / Consistency

### 4.1 Weekly mileage + rolling average

**Methodology.** Sum distance (or duration) per calendar week; rolling 4-week average for trend; week-over-week % change.

**Use in training.** Consistency/gradual-progression tracking; context layer for interpreting every other metric.

**Evidence & limitations.** The research here is more sobering than popular belief. A 2022 systematic review (36 studies, n=23,047 runners) found **conflicting** evidence on weekly distance/duration/frequency/intensity vs. injury and concluded no universal training-progression rule can be issued; a 2018 IJSPT review explicitly found **no evidence for the "10% rule"** (an RCT comparing ~10% vs ~24% weekly increases found no injury difference). The strongest consistent findings: running >64 km/week raises injury risk (van Gent 2007, strong evidence, mainly males), and **a history of prior injury is the single biggest risk factor**. Sudden weekly jumps >30% show a trend toward higher risk (HR 1.59, CI crossing 1) — limited evidence. A JOSPT 2020 commentary argues distance alone is an insufficient training-stress proxy. **Bottom line:** keep weekly volume as a *consistency and progression-smoothness* metric (that's where rolling averages genuinely help) — but do not present mileage or its rate-of-change as a validated injury-risk model.

### 4.2 Rolling 7- and 28-day elevation gain

**Methodology.** Cumulative ascent from GPS/barometric sensor per rolling window.

**Use in training.** Characterizes training context (flat vs hilly routines), load diversity, and gradient stress.

**Evidence & limitations.** There is **no validated threshold** linking cumulative weekly elevation to injury or performance. The biomechanics literature shows grade matters: uphill ≥ ~10–15% raises tibial and Achilles loading, downhill increases patellofemoral stress while reducing tibial load, and real-world runners self-regulate speed/cadence by slope (We-TRAC, n=3001 runs). Trail runners sustain 2–4× the injury incidence of road runners (10.7–19.6 vs 2.5–5.8 per 1,000 h). Gradient is a legitimate *confounder* — it distorts pace-based metrics (VO₂max estimate, decoupling, efficiency) because effort vs. speed breaks down on hills. **Bottom line:** keep elevation mainly as (a) a context flag for interpreting other metrics and (b) a load-diversity tracker — not as a standalone risk/fitness number.

---

## Cross-cutting implications for the v2 tool

1. **Tier the metrics by evidential strength:**
   - **Strong trend metrics:** VO₂max trend, threshold HR/CS (manually anchored), near-distance race-prediction trends, weekly volume/consistency, CTL/ATL/TSB as load trajectory (not absolute bands).
   - **Conditional/aggregated:** decoupling (flat-route, ≥6 sessions); HR-based load.
   - **Popular-heuristic / demote:** ACWR/Load Ratio banded as injury prediction; efficiency-factor fields; elevation-as-injury-metric; any single-value determinant claims.
2. **Default to "trend over absolute value"** everywhere — the literature consistently favors within-athlete change over cross-athlete cutoffs (Nakaoka's L-shaped ACWR result, Firstbeat's 5% precision vs. 7–10% individual error, Riegel's calibration failing at the marathon).
3. **Decide: recompute vs. consume Garmin values.** Garmin already ships VO₂max, LT, Training Load, Load Ratio, Training Status, and Race Predictor — but its load/Training Status algorithms are proprietary and not independently validated, while its VO₂max/LT estimates have peer-reviewed error bounds. Recommendation: compute from raw FIT/JSON exports where you can (TRIMP, ACWR, decoupling, volume, elevation) for reproducibility and transparency, and only *ingest* Garmin's VO₂max/LT/Training Status as reference fields with their documented error bars surfaced to the user.
4. **Carry context into the UI:** sensor type (chest strap vs optical), heat/altitude, and route composition must travel with each metric or they'll be misread (decoupling variance, VO₂max HRmax sensitivity, gradient effects).