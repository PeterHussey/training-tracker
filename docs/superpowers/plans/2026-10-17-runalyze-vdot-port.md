# Runalyze VDOT & Paces Port Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port Runalyze's VO2max/VDOT chain verbatim (pace+HR effective VO2max, 30-day shape, race corrector, marathon preparedness %, race prognosis, percentage paces) into the tracker's pure `metrics/` architecture, wired through pipeline/session/dashboard.

**Architecture:** Five new pure metric modules mirror Runalyze's legacy PHP classes 1:1 (math + qualifiers), plumbing flows through `normalize.py` → `pipeline.compute_metric_rows` → `store.metric_series` → `session.py` views → three distributed dashboard surfaces (layout "B"). Race anchoring reads the Garmin `eventType.typeKey` ("race") already present in the list payload.

**Tech Stack:** Python 3.11+, pandas, stdlib math; no new dependencies. Streamlit/plotly for surfaces (existing).

**Spec:** `docs/superpowers/specs/2026-10-17-runalyze-vdot-port-design.md` (commits b3bf0e0 + 5033778). **Where plan and Runalyze PHP disagree, the PHP at the spec's clone path is the spec** — re-clone if missing:
`git clone --depth 1 --branch support/4.3.x https://github.com/Runalyze/Runalyze.git /private/var/folders/02/r2yk1_2j15b306873bm6gdt40000gp/T/runalyze-src`

**Attribution (put in every new module docstring):** "Runalyze f4b168d5 effec ported from Runalyze 4.3.x `inc/core/<path>`, AGPL; formulas by Daniels/Gilbert."

## Global Constraints

- Verbatim port: only Runalyze formulas/constants; anything upstream computes as null/invalid stays null/invalid here — never filler.
- Constants (verbatim): shape window 30 d; corrector best-3; marathon-shape weights 0.67/0.33; windows 182 d week-km (min 70) / 70 d long-jog; min long jog 13 km; VO2max floor 25.0 for targets; prognosis bounds [15, 90]; prognosis bisection range [2·60·dist, 10·60·dist]; elevation correction ±2/−1 %/100 m, **default OFF**; manual corrector optional param 0.5–2.0, no UI.
- HRmax: divide `avg_hr` by `profile.hrmax` as the repo resolves it (hrmax_source convention unchanged; spec decision 7).
- Qualifying activities (spec): sport mapped "running" or "treadmill" (Garmin typeKeys `running`/`treadmill_running`), distance > 0, moving time > 0, avg HR present and ≤ profile HRmax, DanielsGilbert velocity guard in-bounds [1.2 m/s → 16.67 m/min? no — velocity ∈ [50, 1000] m/min]. All five computations share this filter.
- Metric keys (dotscored): `vdot.shape`, `marathon.shape_pct`, `prognosis.1`, `prognosis.3`, `prognosis.5`, `prognosis.10`, `prognosis.21.1`, `prognosis.42.2`, `prognosis.50`, damped twins `prognosis.<dist>_damped`. Paces table: display-only, never stored.
- Emission rule: a date-keyed series key emits **no row at all** (not a zero row) when its day-count of qualifying inputs is 0; handled by hiding in the dashboard.
- Runalyze `round()` is half-up; use `floor(x + 0.5)` helper in `metrics/daniels_gilbert.py` for pace rounding, never Python banker's `round`.
- Repo style: ruff (line 100), ruff format --check, mypy, `pytest -p no:cacheprovider -q`; new test files `tests/test_<module>.py`; fixtures in `tests/fixtures/`.
- Commits per task; conventional messages; no unrelated refactors.

## Review Focus

The five failure modes most likely to hit a real user, each pinned by a test in the owning task:

1. **Fake filler for empty windows** — zero qualifying activities must produce no rows / hidden surfaces (spec anti-goal). → Task 4 & 8 tests.
2. **HR-dirty data silently skewing everything** — missing/>HRmax avg_hr must exclude, never clamp. → Task 3 & 4 tests.
3. **Elapsed vs moving time in race ratios** — corrector must use moving `duration_s`, not `elapsed_s`. → Task 3 test.
4. **PHP vs Python rounding / floor drift** in paces + marathon-shape days-ago. → Task 2 & 5 tests.
5. **Window off-by-one** (July day boundary `BETWEEN t-30d AND t`, days-ago `floor`, strict `>` window start). → Task 4 & 5 tests.

---

### Task 1: `metrics/numerics.py` — Runalyze Bisection verbatim

**Files:**
- Create: `metrics/numerics.py`
- Test: `tests/test_numerics.py`

**Interfaces (produces, used by Task 6/6b):**
- `find_value(f: Callable[[float], float], target: float, lower: float, upper: float, max_iterations: int = 100, epsilon: float = 0.01) -> float`

Port of `inc/core/Mathematics/Numerics/Bisection.php`: `MaxIterations=100`, `Epsilon=0.01` (a **value** epsilon, not seconds); initial `Current = (lower+upper)/2`; `decreasing_factor = 1 if f(lower) <= f(upper) else -1`; loop up to `max_iterations`: `value = f(Current)`; if `abs(value - target) < epsilon` break; elif `decreasing_factor * value > decreasing_factor * target`: `upper = Current` else `lower = Current`; `Current = (lower+upper)/2`. **Return `Current` (converged last probe), never a midpoint recomputed after the loop.**

- [ ] Write `tests/test_numerics.py` covering BisectionTest goldens:

```python
def test_identity_target_5_exact():
    assert numerics.find_value(lambda x: x, 5.0, 0, 10) == 5.0

def test_iteration_exhaust_returns_current_not_midpoint():
    # 4 iterations on [0,10] converging on 1: sequence 5,2.5,1.25,0.625 ->
    # 0.625 breaks to lower? verify sequence stops with returned Current == 0.9375
    assert numerics.find_value(lambda x: x, 1.0, 0, 10, max_iterations=4) == 0.9375

def test_full_iterations_converge_within_epsilon():
    assert abs(numerics.find_value(lambda x: x, 1.0, 0, 10) - 1.0) < 0.01

def test_cubic_target_7_converges_to_1():
    assert abs(numerics.find_value(lambda x: x**3 + 6, 7.0, -50, 20) - 1.0) <= 0.01
```

(These are Runalyze's own `BisectionTest` numbers — if 0.9375 doesn't come out, re-read `Bisection.php` and match ITS loop order, do not "fix" the constant.)

- [ ] Run: `pytest tests/test_numerics.py -v` → fail (module missing)
- [ ] Implement `metrics/numerics.py` (binary-search loop above + Module docstring with attribution)
- [ ] Run: `pytest tests/test_numerics.py -v` → pass
- [ ] `ruff check metrics/numerics.py tests/test_numerics.py` → clean
- [ ] Commit: `git add metrics/numerics.py tests/test_numerics.py && git commit -m "feat: port Runalyze Bisection (inc/core/Mathematics/Numerics/Bisection.php)"`

### Task 2: `metrics/daniels_gilbert.py` — Dean & Gilbert formula + VO2maxVelocity inverse (port)

**Files:**
- Create: `metrics/daniels_gilbert.py`
- Test: `tests/test_daniels_gilbert.py`

**Interfaces (produces):**

```python
def estimate_from_velocity(v_m_per_min: float) -> float: ...
def evaluate_drop_dead(t_min: float) -> float: ...
def estimate_from_race_result(distance_km: float, seconds: float) -> float: ...
def velocity_from_vo2max(vo2max: float, percentage: float = 1.0) -> float: ...
def pace_s_per_km(vo2max: float | None, percentage: float = 1.0) -> int: ...
def round_half_up(x: float) -> int: ...     # share this everywhere (Review Focus 4)
```

Verbatim from `Sports/Running/VO2max/Estimation/DanielsGilbertFormula.php` and `Sports/Running/VO2max/VO2maxVelocity.php`:

```python
# DG
blocked: 0.0 outside velocity guard  50 <= v_m_per_min <= 1000 (m/min)
estimate_from_velocity(v): max(0.0, -4.6 + 0.182253*v + 0.000104*v*v)
evaluate_drop_dead(t_min): 0.8 + 0.1894393*exp(-0.012778*t_min) +
                                0.2989558*exp(-0.1932605*t_min)
estimate_from_race_result(km, s):
    guard km <= 0 or s <= 0 -> 0.0
    v = 1000.0*km / (s/60.0)          # m/min
    guard 50.0 <= v <= 1000.0 else 0.0
    else estimate_from_velocity(v) / evaluate_drop_dead(s/60.0)
# VO2maxVelocity
velocity(vo2max, percentage=1.0):
    v = vo2max*percentage
    if v <= 0 -> 0.0
    else -876.0 + sqrt(876.0**2 + (4.6 + v)/0.000104)
pace_s_per_km(vo2max, percentage=1.0):
    vel = velocity(vo2max, percentage)
    0 if vel <= 0 else round_half_up(60000.0/vel)   # PHP round() = half-up
```

- [ ] Write tests pinning goldens (numbers from Runalyze `JD\LegacyEffectiveVO2maxTest` + direct transcribe of DG constants):

```python
@pytest.mark.parametrize("vdot, expected_pace", [(30, 1840), (35, 1620), (40, 1448),
    (45, 1310), (50, 1197), (55, 1102), (60, 1023), (65, 954), (70, 895), (75, 843),
    (80, 798), (85, 757)])
def test_pace_5k_is_upstream_table(vdot, expected_pace):
    # estimate_from_race_result(5.0, expected_pace) must land on vdot (±0.1)
    got = daniels_gilbert.estimate_from_race_result(5.0, float(expected_pace))
    assert abs(got - vdot) <= 0.1
    # and the generated pace from the VO2max land within 0.1 s of table (±1 s ok)
    assert abs(daniels_gilbert.pace_s_per_km(vdot) - expected_pace) <= 2
# same parametrize for 10k (30:3826 35:3363 40:3003 45:2716 50:2481 55:2286
#   60:2122 65:1981 70:1860 75:1754 80:1661 85:1579)
# HM 21.0975 (30:8064 35:7453 40:6659 45:6020 50:5495 55:5058 60:4689 65:4373
#   70:4101 75:3863 80:3654 85:3470)
# M 42.195 (30:17357 35:15363 40:13785 45:12506 50:11449 55:10561 60:9805
#   65:9155 70:8590 75:8095 80:7658 85:7270)
def test_velocity_guard_bounds():
    assert daniels_gilbert.estimate_from_velocity(49.99) == 0.0
    assert daniels_gilbert.estimate_from_velocity(50.0) != 0.0
    assert daniels_gilbert.estimate_from_velocity(1000.0) != 0.0
    assert daniels_gilbert.estimate_from_velocity(1000.01) == 0.0
def test_zero_inputs_guard():
    assert daniels_gilbert.estimate_from_race_result(0.0, 100.0) == 0.0
    assert daniels_gilbert.estimate_from_race_result(5.0, 0.0) == 0.0
def test_round_half_up_not_bankers():
    assert daniels_gilbert.round_half_up(1840.5) == 1841
```

- [ ] Run `pytest tests/test_daniels_gilbert.py -v` → fail
- [ ] Implement `metrics/daniels_gilbert.py` (verbatim math above)
- [ ] Run again → pass
- [ ] ruff + mypy on touched files → clean
- [ ] Commit: `feat: port Runalyze DanielsGilbertFormula + VO2maxVelocity`

### Task 3: `metrics/effective_vo2max.py` — legacy per-activity estimate + corrector (port)

**Files:**
- Create: `metrics/effective_vo2max.py`
- Test: `tests/test_effective_vo2max.py`

**Interfaces (produces; consumed by Tasks 4, 8):**

```python
def percentage_at(hr_in_percent: float) -> float: ...
def hr_at(p: float) -> float: ...
def from_pace_and_hr(distance_km: float, seconds: float, hr_in_percent: float,
                     corrector: float = 1.0) -> float: ...
def race_vdot_by_time(distance_km: float, seconds: float) -> float: ...   # aliases DG
def corrector_factor(race_ratios: list[float]) -> float: ...
def race_ratio(race_activity_done: ...) -> trace: ...   # helper: vdot_by_time / training_vdot, ≤0 -> skip
def manual_corrector_param(value: float) -> float ...  # optional param clamp 0.5–2.0, no UI
```

Port of `inc/core/Calculation/JD/LegacyEffectiveVO2max.php` + `LegacyEffectiveVO2maxCorrector.php`:

```python
# guards first: hr_in_percent <= 0 or seconds == 0 or distance <= 0 -> 0.0
speed_really_achieved = 60_000.0 * distance_km / seconds       # m/min
percentage = percentage_at(hr_in_percent)
speed_at_100pct = speed_really_achieved / percentage
value = daniels_gilbert.estimate_from_velocity(speed_at_100pct)  # DG guard applies
exact_value = corrector * value if corrector is not None else value
# corrector at 0.0/None behaves PHP-style: PHP truthiness 0.0 -> Constructor uses
# Static GlobalFactor 1.0; keep Python explicit: corrector <= 0.0 -> 1.0
percentage_at(h): exp((h - 1.00466) / 0.68725)
hr_at(p): 0.68725*ln(p) + 1.00466
# corrector
# race_vdot_by_time = daniels_gilbert.estimate_from_race_result(dist, seconds)
# race_training_vdot = from_pace_and_hr(dist, seconds, avg_hr/hrmax) (corrector=1)
# best_three_by_race_vdot ratio list -> factor = max(ratios) over BEST 3 sorted by
#   race_vdot DESC; ratio <= 0 rows dropped; empty -> 1.0
```

- [ ] Tests, with upstream numbers (`LegacyEffectiveVO2maxTest`):

```python
def test_distance_independence():
    # upstream: 10 km / 2400 s / hr 0.75 == 3 km / 720 s / hr 0.75
    assert effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75) == pytest.approx(
        effective_vo2max.from_pace_and_hr(3.0, 720, 0.75), abs=1e-6)

def test_hr_zero_is_null_not_zero_output():
    assert effective_vo2max.from_pace_and_hr(5.0, 1500, 0.0) == 0.0

def test_corrector_zero_behaves_php_truthiness():
    # 0.0 means "unset" in the PHP constructor → falls back to GlobalFactor 1.0
    assert effective_vo2max.from_pace_and_hr(5.0, 1500, 0.75, corrector=0.0) == \
        effective_vo2max.from_pace_and_hr(5.0, 1500, 0.75)

def test_corrector_multiplies():
    base = effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75)
    assert effective_vo2max.from_pace_and_hr(10.0, 2400, 0.75, corrector=0.9) \
        == pytest.approx(0.9 * base, abs=1e-9)

def test_corrector_factor_best_three_by_race_vdot_default_one():
    # race_vdots with ratios: 60/62=0.9677, 55/53=1.0377, 70/50=1.4, 68/60=1.1333
    ratios = [1.4, 1.1333, 0.9677, 1.0377]
    # best 3 by race vdot: 70, 68, 60 → ratios 1.4, 1.1333, .9677 → max 1.4
    assert effective_vo2max.corrector_factor(ratios, race_vdots=[70,68,60,55]) == pytest.approx(1.4)
    assert effective_vo2max.corrector_factor([], []) == 1.0

def test_race_ratio_uses_moving_seconds():
    # assist: ratio(moving) != ratio(elapsed): race 10 km moving 2400 s, elapsed 2500 s,
    # avg hr 0.75 → ratio_by_moving differs from ratio_by_elapsed (Review Focus 3)
```

- [ ] Run → fail
- [ ] Implement `metrics/effective_vo2max.py`
- [ ] Run → pass
- [ ] ruff + mypy
- [ ] Commit: `feat: port Runalyze LegacyEffectiveVO2max + corrector`

### Task 4: `metrics/vdot_shape.py` — daily effective VO2max + 30-day shape (port)

**Files:**
- Create: `metrics/vdot_shape.py`
- Test: `tests/test_vdot_shape.py`

**Interfaces (produces; consumed by Task 8):**

```python
def qualifying(activities: list[Activity], hrmax: float | None) -> list[Activity]
def activity_vo2max(a: Activity, hrmax: float, corrector: float = 1.0) -> float
def shape_at(activities: list[Activity], as_of: datetime, hrmax: float,
             window_days: int = 30, corrector: float = 1.0) -> float   # 0.0 empty
def shape_series(activities: list[Activity], hrmax: float | None,
                 window_days: int = 30, corrector: float = 1.0) -> pd.Series
```

Port of `inc/core/Calculation/JD/Shape.php`:

```python
# qualifying (spec): sport in {'running','treadmill'} AND distance_km > 0 AND
#   duration_s > 0 AND avg_hr is not None AND avg_hr <= hrmax AND DG velocity
#   (60_000*km/s without HR correction) within [50,1000] m/min.
# activity_vo2max = effective_vo2max.from_pace_and_hr(dist, moving_s, avg_hr/hrmax, corrector)
# shape_at: window BETWEEN window_start AND as_of: rows must satisfy
#   (as_of_ts - activity_ts) <= window_days*86400   AND activity_ts <= as_of_dayend
# value = SUM(vo2max_i*s_i) ; denom = SUM(s_i); shape = round(value/denom, 5)
#   denominator == 0 -> 0.0
# corrected = corrector * shape    (already applied via activity_vo2max in our port)
```

Notes (fidelity to ShapeTest): if two activities the same day, both count with their own durations; historic value gets corrector per-day = factor (a global as-of-now value, faithful to Runalyze's GlobalFactor era, per builder-check note).

- [ ] Tests (ShapeTest-ported numbers):

```python
# three rows same day: durations 1,2,1 s; vo2max 50,60,80, use all → 62.5? No —
# run-weighted: 50*1 + 60*2 + 80*1 over 4 = 62.5. Check the actual basic case:
def test_shape_weighted_mean_same_day(): ...      # inputs [(s=1,50),(s=2,60),(s=1,80)] -> 62.5
def test_use_zero_row_dropped(): ...              # row with duration 0 (non-qualifying) excluded
def test_outside_window_dropped(): ...            # 10-day-old activity outside 5-day window
def test_empty_window_is_zero_not_filler():       # Review Focus 1
    assert shape_at([], now, hrmax) == 0.0
def test_hr_over_hrmax_excluded(): ...            # avg_hr=190, hrmax=185 → excluded (RF 2)
def test_corrector_scales(): ...                  # 50*0.9 == 45
def test_elevation_variant_goes_up_or_down(): ... # +2%/100m up, -1%/100m down (off by default param)
```

Use synthetic Activities (dataclass from `normalize`), no fixture file needed.

- [ ] Run → fail
- [ ] Implement module
- [ ] Run → pass
- [ ] ruff + mypy
- [ ] Commit: `feat: port Runalyze JD\Shape as metrics/vdot_shape.py`

### Task 5: `metrics/marathon_shape.py` — BasicEndurance / MarathonShape (port)

**Files:**
- Create: `metrics/marathon_shape.py`
- Test: `tests/test_marathon_shape.py`

**Interfaces (produces; consumed by Task 8):**

```python
def target_week_km(vdot: float) -> float ...            # pow(max(vdot, 25.0), 1.135)
def target_lj_km_week_real(vdot: float) -> float ...    # ln(max(vdot,25.0)/4)*12  (real)
def target_lj_km_week_reduced(vdot: float) -> float ... # real − 13
def longjog_points(runs: list[tuple[float, int]], vdot: float,
                   days_for_longjogs: int = 70, min_km: float = 13.0) -> float
def week_km(runs: list[tuple[float, int]], ...total over window...) -> float
def percentage(runs_list, vdot, ..., pct_week: float = 0.67, pct_lj: float = 0.33,
               days_for_week_km: int = 182) -> int   # round(100*(pct_week*week_frac + pct_lj*lj_frac))
```

Port of `inc/core/Calculation/BasicEndurance.php` + `Sports/Running/MarathonShape.php` (wrapper `getShapeFor`), strictly:

```python
# long-jog: rows with distance > 13 km inside STRICT window:  activity_ts > ts - 70*86400
# points_i = (2 - (2/70)*floor((ts - ts_i)/86400)) * ((dist_i - 13)**2 / reduced_target**2)
#   (reduced target below floor 25)
# week-km: activity_ts > ts - days_for_week_km*86400; days_for_week_km clamp:
#   days_since_first = ceil((ts - first_ts)/86400);  wp_days = min(182, max(70, days_since_first))
# week_frac = week_km * 7 / wp_days / target_week_km(vdot)
# lj_frac   = points // 1 * 7 / 70            # total*7/DAYS_FOR_LONGJOGS
# pct = round_half_up(100*(pct_week*week_frac + pct_lj*lj_frac))
# NOTE: targets resolve from CURRENT vdot even for historical as-of dates (spec §6 my-call).
```

- [ ] Tests pinning **BasicEnduranceTest + MarathonShapeTest goldens** (do not derive independently — port numbers verbatim):

```python
@pytest.mark.parametrize("vdot, week_env, lj_real", [(30, 47.5, 24), (40, 66.0, 27.5),
    (50, 85.0, 30.0), (60, 104.0, 32.5), (70, 124.0, 34.5), (80, 145.0, 36.0)])
def test_targets_match_upstream(vdot, week_env, lj_real):
    assert marathon_shape.target_week_km(vdot) == pytest.approx(week_env, abs=1.0)
    assert marathon_shape.target_lj_km_week_real(vdot) == pytest.approx(lj_real, abs=1.0)

def test_soft_peaks_to_unrealistic_inputs():        # vdot 1.0 & 20.0 both target floor 25
    assert marathon_shape.target_week_km(1.0) == pytest.approx(37.99, abs=1.0)
    assert marathon_shape.target_lj_km_week_real(1.0) == pytest.approx(21.99, abs=0.1)

def test_marathon_shape_full_scenario_from_upstream():
    # BasicEnduranceTest testForVO2max30: vdot=30, window 7/7 d, min_longjog 10 km,
    # pct_week .67 / pct_lj .33, first activity 7 d ago:
    # runs ages/durations: day 1 (t-1d): 15 km; day 3: 15 km; day 8: 42 km
    # -> week_km 30; longjogPoints 0.36 ± .01; week_frac 0.63 ± .01; total 54%
    pct = marathon_shape.percentage(runs, vdot=30.0)
    assert pct == 54
```

(If 54 doesn't reproduce, re-read `BasicEnduranceTest.php` against the clone — the PHP is the spec; do not relax the constant.)

Also port `MarathonShapeTest` shapeFor goldens as a cross-check with pct_week=0.50:

```python
def test_shape_for_marathonshape_test_cases():
    # weekly = 52 km over 1 day, points 1, vdot 60, 7-day windows, pct .5/.5 → 75
    assert marathon_shape.percentage(...) == 75
    # (104, 0.5) → 75; (104, 1) → 100; (52, 2) → 125; (0, 2) → 100
```

- [ ] → TDD cycle on the above
- [ ] Commit: `feat: port Runalyze BasicEndurance/MarathonShape as metrics/marathon_shape.py`

### Task 6: `metrics/prognosis.py` — race prognosis bisection (port)

**Files:**
- Create: `metrics/prognosis.py`
- Test: `tests/test_prognosis.py`

**Interfaces (produces; consumed by Task 8):**

```python
DISTS: list[float] = [1.0, 3.0, 5.0, 10.0, 21.1, 42.2, 50.0]
REASONABLE_MIN: float = 15.0
REASONABLE_MAX: float = 90.0

def adjustment_factor(distance_km: float, shape_pct: float | None) -> float
def prognosis_seconds(vdot: float, distance_km: float,
                      marathon_shape_pct: float | None = None) -> float | None
def prognosis_table(vdot, marathon_shape_pct=None) -> list[dict]  # per dist raw + damped seconds
```

Port of `inc/core/Sports/Running/Prognosis/VO2max.php` (+ `Numerics/Bisection`):

```python
if not REASONABLE_MIN <= vdot <= REASONABLE_MAX: return None
adjusted = vdot * adjustment_factor(distance_km, shape_pct) if shape_pct is not None else vdot
   # where adjustment_factor = min(1.0, 0.6 + 0.4*max(0.0, 1 - (dist**1.23 - shape_pct)/100.0))
bisection target: daniels_gilbert.estimate_from_race_result(distance_km, s) == adjusted
range s ∈ [2*60*dist, 10*60*dist]; return numerics.find_value(...)
```

- [ ] Tests pinning upstream numbers (damped factor closed-form checks + estimate-from-race round-trip):

```python
@pytest.mark.parametrize("vdot", [14.9, 90.1])
def test_bounds_return_none(vdot): assert prognosis.prognosis_seconds(vdot, 5.0) is None

@pytest.mark.parametrize("vdot, dist, expected_s", [(30, 5, 1840), (50, 10, 2481),
    (60, 21.0975, 4689), (60, 42.195, 9805)])
def test_raw_prognosis_matches_fromPace_golden(vdot, dist, expected_s):
    # inverse of the DG estimate must land on the same number the DG formula
    # produces for (dist, expected_s) — bisection converges within ±2 s
    got = prognosis.prognosis_seconds(vdot, dist)
    assert got == pytest.approx(expected_s, abs=2)

def test_adjustment_factor_shape_100_is_one():
    assert prognosis.adjustment_factor(42.2, 100.0) == pytest.approx(1.0)
def test_adjustment_factor_shape_zero_half_marathon():
    assert prognosis.adjustment_factor(21.1, 0.0) == pytest.approx(0.8301, abs=0.001)
def test_damped_prognosis_is_slower_than_raw():
    raw = prognosis.prognosis_seconds(60, 42.2)
    damped = prognosis.prognosis_seconds(60, 42.2, marathon_shape_pct=0.0)
    assert damped > raw
```

- [ ] → TDD cycle
- [ ] Commit: `feat: port Runalyze Prognosis\VO2max as metrics/prognosis.py`

### Task 7: Race anchoring plumbing — `normalize.py` + `store.py`

**Files:**
- Modify: `normalize.py:40-56` (sport_of area — add `event_type_of`) and `normalize.py:113-150` (Activity dataclass: add `event_type: str | None = None`), `normalize.py: backbone construction of Activity` (~L183: add `event_type=...`)
- Modify: `store.py` (activities schema: add nullable `event_type TEXT`; migration + save/load)
- Test: `tests/test_normalize.py` (append), `tests/test_store.py` (append)

**Interfaces (produces):** `Activity.event_type: str | None` carrying `summary["eventType"]["typeKey"]` (verified present in `tests/fixtures/activities_sample.json`, e.g. `"uncategorized"`). Store roundtrip persists it. No fingerprint change needed beyond the natural schema-gated reload.

- [ ] Add failing normalize test: fixture activity #0 asserts `event_type == "uncategorized"` and an untagged run with `eventType.typeKey == "race"` roundtrips `"race"`.
- [ ] Restore favors fixture race-tag: add one synthetic dict payload case (copy fixture shape) with `"eventType": {"typeId": 9, "typeKey": "race", ...}` asserting `a.event_type == "race"`.
- [ ] Store: fail-first test — save activities (one race-tagged), load, assert event_type survives `INSERT OR REPLACE` reload.
- [ ] Implement both.
- [ ] Run tests → pass
- [ ] Commit: `feat: carry Garmin event-type through normalize + store for race anchoring`

### Task 8: Pipeline + session + registry wiring

**Files:**
- Modify: `pipeline.py` (~L30 `compute_metric_rows`, ~L355 persistence path untouched), `session.py:31-50` (EXPECTED/CONDITIONAL lists), `garmin_fields.py` (METRICS frozenset + `GarminField` entry for `eventType.typeKey`), `e2e_report.py` only if its metric contract needs new conditional entries
- Test: `tests/test_pipeline.py` (append), `tests/test_session.py` (append)

**Interfaces (Consumes from prior tasks):** `daniels_gilbert.*`, `effective_vo2max.*`, `vdot_shape.*`, `marathon_shape.*`, `prognosis.*`, `Activity.event_type`.

Wiring spec:

```python
# pipeline.compute_metric_rows adds (after existing families):
hrmax = profile.hrmax (via the same hrmax_source path other metrics use)
races = [a for a in activities if a.event_type == "race" and qualifying by spec]
# per race: ratio = race_vdot_by_time(dist, moving_s) / from_pace_and_hr(dist, moving_s, avg_hr/hrmax)
corrector = effective_vo2max.corrector_factor(ratios, race_vdots=[...])  # 1.0 when no races
rows += vdot_shape rows:   key "vdot.shape",  daily Series over [first activity date, last]
rows += marathon rows:     key "marathon.shape_pct" — as-of vdot = that day's corrected
                           shape (Runalyze-faithful; spec §6 my-call)
rows += prognosis rows:    key "prognosis.<dist>" (raw) AND "prognosis.<dist>_damped"
                           (with shape_pct input per day)
# all empty-window -> no rows (Global constraint) — the shape series itself never emits
#   a day before its first qualifying activity
```

- `garmin_fields.py` additions:

```python
# METRICS frozenset += {"vdot_shape", "marathon_shape", "prognosis"}
# GarminField(name="eventType.typeKey", endpoint="activity_list", units="enum",
#   description="Garmin Connect event tag; 'race' anchors the corrector (Runalyze port)",
#   metrics=("vdot_shape",), limitations="Known only if the user tagged it in Garmin Connect.")
```

- `session.py`: add `vdot.shape`, `marathon.shape_pct`, `prognosis.<dist>` (+ damped) to CONDITIONAL_METRICS with reason strings (feature: "needs qualifying runs with HR data" / "needs tagged races"); do NOT put in EXPECTED_METRICS (would break e2e on first run before races are tagged).

- [ ] Tests:

```python
def test_pipeline_emits_vdot_shape_with_qualifying_runs():
    # synthetic: 3 outdoor runs w/ HR + 1 tread + 1 cross → series present, dates match
def test_pipeline_omits_prognosis_when_no_qualifying():
    # only strength/cross activities → no prognosis.* rows, no marathon rows (RF 1)
def test_corrector_factor_comes_from_race_tagged_activities():
    # two race-tagged runs + 3 training runs; shape_corrected == raw_shape * factor
def test_session_conditional_contains_new_keys():
```

- [ ] → TDD cycle
- [ ] Commit: `feat: wire Runalyze VDOT/marathon-shape/prognosis families through pipeline+session`

### Task 9: Dashboard surfaces (layout "B": all three, distributed)

**Files:**
- Modify: `dashboard.py` (three anchors: Garmin VO2max chart ~L1124, Garmin race-predictions block ~L1211-1264, and end-of-Overview bottom placement); `tests/test_dashboard.py` (append a smoke)
- Test: `tests/test_dashboard.py` smoke (render function targets, no full app run)

Surfaces spec:

1. **Shape overlay** on the existing Garmin VO2max trend chart block (~L1124): add `vdot.shape` as a second scatter line (distinct color, e.g. orange) on the same figure; caption line: "Runalyze-style shape: 30-day duration-weighted mean of per-run pace+HR VO2max, × race corrector (best-3). Independent of Garmin Firstbeat." Keep the existing VO2max chart caption intact below it.
2. **Prognosis table** directly under the Garmin race-predictions chart (~L1264): `st.dataframe` with columns Distance | Raw | Damped — current-day values for all 7 distances from latest `prognosis.*` rows; when only damped exists because no shape, show raw only. Format seconds `mm:ss`. Hide entirely if `prognosis.5` row absent (RF 1). Caption: "Runalyze-style prognosis ( Daniels-Gilbert, bisection-inverted; damped = marathon-shape-normalized)."
3. **Marathon-shape detail** under or beside the prognosis table: latest `marathon.shape_pct` value + the two long-input targets from `marathon_shape.target_week_km / target_lj_km_week_real` of current shape (only when the column exists).
4. **Paces table** at the **bottom of Overview**, after the last existing section: derived in-session (never stored) via `daniels_gilbert.pace_s_per_km(current_shape rounded, pct)` at pct ∈ {65..110 step 5} → mm:ss/km string column. Uses the **rounded shape** like Runalyze's tools page. Hide when no `vdot.shape` rows.
5. Race-anchoring hint caption once in the prognosis table block: "Races you tag in Garmin Connect (event type `Race`) anchor these once picked up in a historical fetch — incremental refresh won't revisit old activities."

- [ ] Update `tests/test_dashboard.py` with a render-level smoke for each of the three surfaces (fixture with race-tagged synthetic activity)
- [ ] Run full existing dashboard tests → unbroken
- [ ] Commit: `feat: dashboard surfaces — shape overlay, prognosis table, paces table (layout B)`

### Task 10: Docs, gitignore hygiene, full QA

**Files:**
- Modify: `docs/v2/metrics-spec.md` (new families section + attribution + null-behavior note), `.gitignore` (add `.superpowers/`), `README.md` (one line in the metrics section if it enumerates families).

- [ ] Write the spec doc section: formulas bullet summary + pointer to `docs/superpowers/specs/2026-10-17-runalyze-vdot-port-design.md` for verbatim detail; note the display-only paces table and event-type race anchoring (race-day flow: tag in Garmin → historical fetch once).
- [ ] Add `.superpowers/` to `.gitignore` (below `data/`: "Visual-companion/session scratch").
- [ ] Full gate: `pytest -p no:cacheprovider -q` (all pass), `ruff check .` clean, `ruff format --check .` clean, `mypy .` clean, and `.venv/bin/python e2e_report.py` exits 0 (fixture-backed).
- [ ] Commit: `docs+chore: document Runalyze VDOT families; ignore .superpowers scratch`

## Provenance map (spec ↔ task)

| Spec § | Module / surface | Task |
|---|---|---|
| §1 DG formula | `metrics/daniels_gilbert.py` | 2 |
| §2 VO2maxVelocity | `metrics/daniels_gilbert.py` | 2 |
| §3 LegacyEffectiveVO2max | `metrics/effective_vo2max.py` | 3 |
| §4 corrector best-3 | `metrics/effective_vo2max.py` | 3 |
| §5 shape 30d | `metrics/vdot_shape.py` | 4 |
| §6 BasicEndurance | `metrics/marathon_shape.py` | 5 |
| §7 prognosis bisection | `metrics/prognosis.py` | 6 |
| Event-type plumbing | normalize/store | 7 |
| Pipeline/session/registry | pipeline/session/garmin_fields | 8 |
| Layout B surfaces | dashboard | 9 |
| Docs/verification items 1–3 | metrics-spec/.gitignore | 10 |

## Self-review notes (already applied)

- Original damping §7 pseudocode error (divide-seconds) is already corrected in the committed spec — Task 5 (prognosis) implements the corrected order-of-operations (factor × vdot before bisection).
- No plan body implementation beyond what the spec determines; Runalyze's PHP files win over any ambiguous plan step.
- If `php`-CLI is available on this machine, upstream goldens for Task 2/6 may be **re-derived directly from the PHP** — prefer that over typing `print_r` results by hand.

_Physical note: total plan is 10 tasks; expected ±1 task feedback loop; plan covers every spec section, including docs + QA gate._
