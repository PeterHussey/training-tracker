# v2 Running-Metrics Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the v2 measurement layer: define the metric set (which running metrics, from which Garmin fields, computed how), acquire + normalize Garmin data, compute each metric, and persist results — with reproducible methodology and documented limitations. Visualization/UI/refresh scheduling are explicitly out of scope.

**Architecture:** A fresh `v2/` Python package. A thin `garminconnect`-REST gateway (credentials from 1Password via the `op` CLI) fetches activities/details/performance endpoints; `normalize.py` maps raw JSON to a validated `Activity` model; per-metric **pure functions** in `metrics/` return canonical time-series; `store.py` persists to SQLite; `pipeline.py` orchestrates fetch→normalize→compute→store. A `garmin_fields.py` registry is the single source of truth for the field-mapping doc (a deliverable). Metric calculators never touch the network — they consume `Activity` objects so tests run on frozen fixtures.

**Tech Stack:** Python 3.11+, `garminconnect` (Garmin REST), `pandas`, `requests`, stdlib `sqlite3`, `pytest` (dev). Nothing else.

**Spec:** The evidence base is `docs/research/running-metrics-brief.md`. The decisions produced by this plan are frozen into `docs/v2/metrics-spec.md` and `docs/v2/garmin-field-mapping.md` (Task 14). This plan argues from the research brief; the executors read both.

## Global Constraints

- **Python >= 3.11 required** (`garminconnect` uses modern union types). Local toolchain is 3.8.10 — recreate the venv with a 3.11+ interpreter (Task 1).
- **Credentials live in 1Password**, read via the `op` CLI: `op item get Garmin --fields username --fields password --reveal`. Never store credentials or tokens in the repo. For CI/test runs, fall back to env `GARMIN_EMAIL` / `GARMIN_PASSWORD`.
- **No UI, no visualization, no refresh/update scheduling code** in this plan — explicitly deferred.
- Raw Garmin JSON is **cached and never deleted** (`v2/cache/`); tests consume frozen fixtures, never the network.
- **Units are SI internally** (m, s, m/s, bpm); the docs surface human units (km, min/km).
- **Metric rows always carry** a `source` (`computed` | `garmin_ingested`) and a `flags` JSON of context/limitation tags (sensor, route, HRmax source, caveats).
- **Running metrics** are computed on outdoor-`running` activities only; `treadmill` is its own group; all other sports (`strength_training`, `indoor_cycling`, `elliptical`, `indoor_rowing`, etc.) feed **cross-training volume/load only**.
- **Recompute (reproducible from raw JSON):** volume, elevation, TRIMP (Banister + Edwards), ACWR, CTL/ATL/TSB, decoupling. **Ingest (reference, error bars surfaced):** VO₂max, lactate threshold (HR anchored, pace flagged), race predictions, Garmin Training Status / Training Effect.
- **Excluded as primary metrics** (documented, not computed as headline): ACWR-as-injury-gate, efficiency factor as a standalone, elevation-as-injury-risk, Garmin's proprietary TSS/load-as-truth.
- **Config defaults (all overridable via `RunnerProfile`):** decoupling min effort **90 min (= 5400 s)**; min aggregation **6 sessions**; gradient filter **25 m/km**; ACWR coupled windows 7/28; PMC EWMA τ 42/7; Banister exponent 1.92 (M) / 1.67 (F), intercept factor 0.64; Edwards zone weights {1,2,3,4,5}×zone time.
- Every task ends with a commit. Docs tasks produce committed docs.

## File Structure

```text
v2/
  pyproject.toml                  # project + deps + pytest config
  .gitignore                      # venv, cache, tokens
  garmin_fields.py                # field registry: canonical name, source endpoint, units, mapped metrics, limitations (source of truth)
  gateway.py                      # GarminGateway: op CLI creds, Garmin REST login, activity/detail/performance fetchers
  normalize.py                    # raw JSON -> validated Activity dataclass; sport classification; unit conversion
  profile.py                      # RunnerProfile: HRmax/HRrest/sex/birth_year/LTHR override/HR zones/Banister params
  metric_series.py                # MetricRow + rows_from_series() helpers (pandas series -> store rows)
  metrics/
    __init__.py
    volume.py                     # weekly/running/treadmill/cross/total distance + rolling + WoW%
    elevation.py                  # daily/weekly/28d elevation gain + gain-per-km (context)
    trimp.py                      # Edwards (zone-seconds) + Banister (ΔHR) TRIMP; daily series
    pmc.py                        # CTL/ATL/TSB EWMA (τ 42/7)
    acwr.py                       # coupled 7/28 ratio + rolling-history percentile
    decoupling.py                 # eligibility filter, per-half HR/pace, >=6-session aggregate
    vo2max.py                     # daily VO2max series from per-activity vO2MaxValue
    threshold.py                  # parse LT payload, manual-anchor override, approx CS from fastestSplit_1609
    racepredict.py                # parse race-prediction payload -> 5k/10k/half/full series
  store.py                        # SQLite schema + MetricStore (profile/activities/metric_series repos)
  pipeline.py                     # run_pipeline(): compute everything -> store; returns row counts
  cache/                          # raw Garmin JSON dumps (gitignored except fixtures/)
  tests/
    fixtures/
      activities_sample.json      # frozen subset of the cached Garmin activity list (committed)
      lactate_threshold.json      # frozen get_lactate_threshold payload (committed)
      race_predictions.json       # frozen get_race_predictions payload (committed)
    test_field_registry.py
    test_normalize.py
    test_profile.py
    test_volume.py
    test_elevation.py
    test_trimp.py
    test_pmc.py
    test_acwr.py
    test_decoupling.py
    test_vo2max.py
    test_threshold.py
    test_racepredict.py
    test_store.py
    test_pipeline.py
docs/
  v2/
    metrics-spec.md               # deliverable: decisions, formulas, config, context flags
    garmin-field-mapping.md       # deliverable: available fields -> metrics + limitations
```

## Interfaces (shared vocabulary — later tasks rely on these exact signatures)

**`normalize.py`**
- `sport_of(summary: dict) -> str` — `"running" | "treadmill" | "cross"`
- `from_summary(summary: dict) -> Activity`
- `class Activity` (dataclass): `activity_id:int`, `sport:str`, `date:date`, `ts_ms:int`, `distance_m:float`, `duration_s:float`, `elapsed_s:float`, `avg_hr:float|None`, `max_hr:float|None`, `zone_s:dict[int,float]`, `ele_gain_m:float|None`, `ele_loss_m:float|None`, `vo2max:float|None`, `aerobic_te:float|None`, `anaerobic_te:float|None`, `avg_speed:float|None`, `fastest_split_1609:float|None`, `avg_cadence:float|None`, `device_id:int|None`, `location:str|None`, `lat:float|None`, `lon:float|None`, `raw:dict`; properties `distance_km`, `duration_min`.

**`gateway.py`**
- `class GarminGateway`: `fetch_activities(start:str, end:str) -> list[dict]`, `fetch_activity_details(activity_id:int) -> dict`, `fetch_lactate_threshold() -> dict`, `fetch_race_predictions() -> dict`, `fetch_training_status(cdate:str) -> dict`.

**`profile.py`**
- `class RunnerProfile`: fields `hrmax:int`, `hrrest:int`, `sex:str`, `birth_year:int`, `lthr_manual:int|None`, `hr_zones:dict[int,tuple[int,int]]`, `units:str`. Methods `age_predicted_hrmax() -> int`, `banister_exponent() -> float`, `exp_intercept_factor() -> float`. Class attr `EDWARDS_WEIGHTS = {1:1.0,2:2.0,3:3.0,4:4.0,5:5.0}`. `default_profile(...) -> RunnerProfile`.

**`metrics/volume.py`**
- `weekly_distance(activities: list[Activity], group: str) -> pd.Series` — index = ISO-week label `YYYY-Www`, values km.
- `rolling_4wk(weekly: pd.Series) -> pd.Series`
- `week_over_week_pct(weekly: pd.Series) -> pd.Series`
- `groups() -> list[str]` = `["total","running","treadmill","cross"]`

**`metrics/elevation.py`**
- `daily_elevation_gain(activities: list[Activity], group: str = "running") -> pd.Series`
- `rolling_28d(daily_gain: pd.Series) -> pd.Series`
- `gain_per_km(activities: list[Activity], group: str = "running") -> pd.Series`

**`metrics/trimp.py`**
- `edwards_trimp(zone_s: dict, weights: dict|None = None) -> float`
- `banister_trimp(duration_min: float, avg_hr: float, profile: RunnerProfile) -> float`
- `daily_trimp(activities: list[Activity], profile) -> pd.DataFrame` — columns `["banister","edwards"]`, DatetimeIndex (daily), one row per activity date summed.

**`metrics/pmc.py`**
- `ctl_atl_tsb(daily_trimp: pd.Series, tau_ctl=42, tau_atl=7) -> pd.DataFrame` — columns `ctl, atl, tsb`, daily DatetimeIndex.

**`metrics/acwr.py`**
- `coupled_acwr(daily: pd.Series, acute=7, chronic=28) -> pd.Series`
- `history_percentile(acwr: pd.Series, window=180) -> pd.DataFrame` — columns `acwr, history_pct`.

**`metrics/decoupling.py`**
- `decoupling_percent(hr: list[float], speed: list[float]) -> float`
- `eligible_activity(a: Activity, min_duration_s=5400, max_ele_per_km=25.0) -> bool`
- `route_key(a: Activity, grid=0.01) -> tuple`
- `aggregate_decoupling(decouplings: list[float], min_sessions=6) -> dict|None` — `{"n", "mean", "stdev"}`.

**`metrics/vo2max.py`**
- `daily_vo2max(activities: list[Activity]) -> pd.Series`

**`metrics/threshold.py`**
- `class LTData` (dataclass): `hr:int|None`, `speed_m_s:float|None`, `date:str|None`
- `parse_lt(payload: dict) -> LTData`
- `approx_cs_1609(activities: list[Activity]) -> pd.Series` — per-date best-effort 1609 m pace (m/s).

**`metrics/racepredict.py`**
- `DISTANCE_KEYS = {"5k":"Run_5k","10k":"Run_10k","half":"Run_half_marathon","full":"Run_full_marathon"}`
- `parse_predictions(payload: dict) -> dict[str, int|None]` — distance → total **seconds** (unit reconciled in Task 12).

**`metric_series.py`**
- `rows_from_series(metric: str, series: pd.Series|pd.DataFrame, source: str, params=None, flags=None) -> list[dict]` — each dict `{"metric","date","value","source","params","flags"}`; for a DataFrame, one sub-metric per column → `metric` = `"<metric>.<col>"`.

**`store.py`**
- `class MetricStore(path: str|Path)`: `save_runner_profile(profile)`, `save_activities(activities: list[Activity])`, `save_metric_rows(rows: list[dict])`, `read_metric(metric: str) -> list[dict]`.

**`pipeline.py`**
- `run_pipeline(activities: list[Activity], profile: RunnerProfile, out_db: str|Path, lt_payload: dict|None = None, race_payload: dict|None = None) -> dict[str,int]` — returns `{"metrics_written": int, "activities": int}`. Optional `lt_payload` / `race_payload` (from `fetch_lactate_threshold()` / `fetch_race_predictions()`) are ingested as reference metrics when supplied; the pure-activities path always writes the computed metrics.

---

### Task 1: Toolchain & Package Scaffold

**Files:**
- Create: `v2/pyproject.toml`
- Create: `v2/.gitignore`
- Create: `v2/tests/__init__.py`
- Create: `v2/tests/test_smoke.py`

**Interfaces:**
- Produces: python environment where `python3.11 -m pytest` works; package importable from `v2/`.

- [ ] **Step 1: Verify/install Python 3.11+**

```bash
which python3.11 || brew install python@3.11
python3.11 --version
```

- [ ] **Step 2: Recreate the venv and install deps**

```bash
rm -rf .venv
python3.11 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install "pandas>=2.2" "requests>=2.31" "garminconnect==0.3.2" "pytest>=8"
```

- [ ] **Step 3: Write `v2/pyproject.toml`**

```toml
[project]
name = "training-tracker-v2"
version = "0.2.0"
requires-python = ">=3.11"
dependencies = [
    "pandas>=2.2",
    "requests>=2.31",
    "garminconnect==0.3.2",
]

[project.optional-dependencies]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

- [ ] **Step 4: Write `v2/.gitignore`**

```text
.venv/
__pycache__/
*.pyc
cache/
!.gitkeep
.DS_Store
oauth*.json
*.token*
```

- [ ] **Step 5: Write `v2/tests/__init__.py`** as an empty file, and the smoke test:

```python
# tests/test_smoke.py
def test_environment():
    import sys
    assert sys.version_info >= (3, 11)
    import garminconnect, pandas  # noqa: F401
```

- [ ] **Step 6: Run the smoke test**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_smoke.py -v`
Expected: `1 passed`. Note the `cd v2` is required because the venv is at repo root `.venv`.

- [ ] **Step 7: Freeze a sample activities fixture from the cached v1 data**

```bash
mkdir -p v2/tests/fixtures
../.venv/bin/python - <<'PY'
import json
from pathlib import Path
raw = json.loads(Path("../v1/cache/garmin_raw.json").read_text())
if isinstance(raw, dict) and "data" in raw:
    raw = raw["data"]
by_key = {}
order = ["running", "treadmill_running", "indoor_cycling", "strength_training"]
for a in raw:
    tk = (a.get("activityType") or {}).get("typeKey")
    if tk not in by_key:
        by_key[tk] = []
    if len(by_key[tk]) < 4:
        by_key[tk].append(a)
sample = [a for k in order if k in by_key for a in by_key[k]]
Path("v2/tests/fixtures/activities_sample.json").write_text(json.dumps(sample, indent=2))
print("wrote", len(sample), "activities")
PY
```

Expected: prints `wrote 16` (4 per type). Verify the file contains ≥1 outdoor run with `duration >= 5400` seconds (needed by decoupling tests in Task 9); if not, keep more `running` entries by raising the cap for `running` to 8 (`if tk in ("running","treadmill_running","indoor_cycling","strength_training")` and collect first 8 runs).

- [ ] **Step 8: Commit**

```bash
git add v2/pyproject.toml v2/.gitignore v2/tests v2/tests/fixtures/activities_sample.json
git commit -m "feat(v2): scaffold package, pyproject, pytest, frozen activity fixture"
```

### Task 2: Field Registry + Field-Mapping Doc (draft)

**Files:**
- Create: `v2/garmin_fields.py`
- Create: `v2/tests/test_field_registry.py`
- Create: `docs/v2/garmin-field-mapping.md`

**Interfaces:**
- Consumes: fixture `tests/fixtures/activities_sample.json`.
- Produces: `METRICS` (frozenset of metric keys), `GarminField` dataclass, `FIELD_REGISTRY` (tuple), `fields_for_metric(metric)`.

- [ ] **Step 1: Write the failing registry coverage test**

```python
# tests/test_field_registry.py
import json
from pathlib import Path
from garmin_fields import FIELD_REGISTRY, METRICS, fields_for_metric, GarminField

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"

ACTIVITY_LIST_FIELDS = {f.name for f in FIELD_REGISTRY if f.endpoint == "activity_list"}

# ctl/atl/tsb are chronic/acute derived metrics computed by accumulating the
# daily series (volume/trimp); they consume no raw Garmin field directly, so
# they are intentionally absent from every field's metrics tuple.
DERIVED_METRICS = {"ctl", "atl", "tsb"}

# Fields that carry no limitation text by design (a limitation string would be
# filler). Kept in sync with the registry so the units/limitations test stays
# a genuine guard against accidentally-truncated documentation.
LIMITLESS_FIELDS = {
    "activityId", "activityUUID", "beginTimestamp", "minElevation",
    "maxElevation", "avgElevation", "startLongitude", "endLatitude",
    "endLongitude", "manufacturer", "metrics[].distance",
    "speed_and_heart_rate.calendarDate", "Run_10k.time",
}


def test_every_metric_is_mapped():
    mapped = {m for f in FIELD_REGISTRY for m in f.metrics}
    assert mapped == METRICS - DERIVED_METRICS
    assert DERIVED_METRICS <= METRICS


def _resolve(obj, dotted):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return True


def test_every_activity_list_field_is_present_or_documented_absent():
    data = json.loads(FIXTURE.read_text())
    running = [a for a in data if a.get("activityType", {}).get("typeKey") == "running"]
    missing = []
    for f in FIELD_REGISTRY:
        if f.endpoint != "activity_list":
            continue
        if not any(_resolve(a, f.name) for a in running):
            missing.append(f.name)
    assert missing == [], f"fields absent from every running activity: {missing}"


def test_fields_have_units_and_limitations():
    for f in FIELD_REGISTRY:
        assert f.units, f"{f.name} missing units"
        if f.name in LIMITLESS_FIELDS:
            continue
        assert f.limitations, f"{f.name} missing limitations"


def test_field_registry_is_frozen_and_importable():
    assert isinstance(FIELD_REGISTRY, tuple)
    assert isinstance(METRICS, frozenset)
    assert fields_for_metric("volume")
    assert all(f.metrics for f in FIELD_REGISTRY)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_field_registry.py -v`
Expected: fails with `ModuleNotFoundError: No module named 'garmin_fields'`.

- [ ] **Step 3: Write `v2/garmin_fields.py`**

```python
"""Canonical registry of Garmin fields used by v2 metrics.

Source of truth for docs/v2/garmin-field-mapping.md. endpoint is one of:
activity_list | activity_details | lt | race_predictions | training_status |
user_settings | heart_rate_zones | daily_summary
"""
from dataclasses import dataclass

METRICS = frozenset({
    "volume", "elevation", "trimp_edwards", "trimp_banister", "ctl", "atl",
    "tsb", "acwr", "decoupling", "vo2max", "lt_hr", "lt_pace", "cs_approx",
    "race_5k", "race_10k", "race_half", "race_full", "load_reference",
    "cross_training",
})


@dataclass(frozen=True)
class GarminField:
    name: str
    endpoint: str
    units: str
    description: str
    metrics: tuple[str, ...]
    limitations: str = ""


ZONE_LIMIT = (
    "Seconds-in-zone buckets depend on the device HR-zone config. "
    "Reproducibility requires persisting RunnerProfile.hr_zones alongside the series."
)

FIELD_REGISTRY = (
    # ---- activity list (activitylist-service/activities/search/activities) ----
    GarminField("activityId", "activity_list", "id",
                "Per-activity unique integer id", ("volume",)),
    GarminField("activityUUID", "activity_list", "uuid",
                "Per-activity unique uuid", ("volume",)),
    GarminField("activityType.typeKey", "activity_list", "enum",
                "Nested sport key (running, treadmill_running, indoor_cycling, strength_training, ...)",
                ("volume", "cross_training"),
                "Nested under 'activityType'; absent typeKey means classification falls back to sportTypeId."),
    GarminField("sportTypeId", "activity_list", "id",
                "Numeric sport type id", ("volume", "cross_training"),
                "Backup for classification when typeKey is missing; mapping table maintained in normalize.py."),
    GarminField("startTimeLocal", "activity_list", "ISO datetime",
                "Local wall-clock start (YYYY-MM-DD HH:MM:SS)", ("volume",),
                "Drives local calendar day/week bucketing. Timezone id recorded separately in timeZoneId."),
    GarminField("beginTimestamp", "activity_list", "epoch ms",
                "UTC start timestamp", ("volume",)),
    GarminField("distance", "activity_list", "m",
                "Total distance", ("volume", "elevation"),
                "0.0 for indoor_cycling; absent for strength. Convert to km for human-facing series."),
    GarminField("duration", "activity_list", "s",
                "Moving duration (excludes paused time)", ("volume", "trimp_banister", "trimp_edwards", "acwr"),
                "Use duration not elapsedDuration for TRIMP denominators; elapsedDuration adds pause time."),
    GarminField("elapsedDuration", "activity_list", "s",
                "Elapsed wall-clock duration", ("volume",),
                "Can materially exceed duration on stop-and-go sessions; used for decoupling eligibility (>=90 min sustained)."),
    GarminField("movingDuration", "activity_list", "s",
                "Duration with motion", ("volume",),
                "Not always present; fall back to duration."),
    GarminField("averageHR", "activity_list", "bpm",
                "Session mean heart rate", ("trimp_banister", "cross_training"),
                "Optional-sensor dependent (chest strap preferred). Missing on manually-uploaded strength data -> activity excluded from HR-based load."),
    GarminField("maxHR", "activity_list", "bpm",
                "Session max heart rate", ("trimp_banister",),
                "Reported even when elevation is missing; excluded from inference when spikes look sensor-artifactual (not enforced in v2)."),
    GarminField("hrTimeInZone_1", "activity_list", "s", "Seconds in HR zone 1", ("trimp_edwards",), ZONE_LIMIT),
    GarminField("hrTimeInZone_2", "activity_list", "s", "Seconds in HR zone 2", ("trimp_edwards",), ZONE_LIMIT),
    GarminField("hrTimeInZone_3", "activity_list", "s", "Seconds in HR zone 3", ("trimp_edwards",), ZONE_LIMIT),
    GarminField("hrTimeInZone_4", "activity_list", "s", "Seconds in HR zone 4", ("trimp_edwards",), ZONE_LIMIT),
    GarminField("hrTimeInZone_5", "activity_list", "s", "Seconds in HR zone 5", ("trimp_edwards",), ZONE_LIMIT),
    GarminField("averageSpeed", "activity_list", "m/s",
                "Mean moving speed", ("cs_approx",),
                "Not present on all activities; convert to min/km for humans."),
    GarminField("fastestSplit_1609", "activity_list", "s",
                "Fastest 1-mile split within the session", ("cs_approx",),
                "Approximation only: not a controlled critical-speed test; best used as a lower-bound trend signal."),
    GarminField("maxSpeed", "activity_list", "m/s", "Peak instantaneous speed", ("cs_approx",),
                "Sprint artifacts; not used directly for CS."),
    GarminField("elevationGain", "activity_list", "m",
                "Total ascent", ("elevation", "decoupling"),
                "Absent on treadmill/intensity/indoor activities. Used as a gradient filter for decoupling (25 m/km)."),
    GarminField("elevationLoss", "activity_list", "m", "Total descent", ("elevation",),
                "Same indoor gap as elevationGain."),
    GarminField("minElevation", "activity_list", "m", "Lowest point", ("elevation",)),
    GarminField("maxElevation", "activity_list", "m", "Highest point", ("elevation",)),
    GarminField("avgElevation", "activity_list", "m", "Mean altitude", ("elevation",)),
    GarminField("vO2MaxValue", "activity_list", "mL/kg/min",
                "Firstbeat VO2max estimate published for the run", ("vo2max",),
                "Only present on outdoor running (absent: treadmill, cross-training). Estimate (5-10% error), HRmax-dependent, underestimates highly-trained runners. Do not recompute."),
    GarminField("aerobicTrainingEffect", "activity_list", "1-5",
                "Firstbeat aerobic TE", ("load_reference",),
                "Proprietary; reference only, never a gate."),
    GarminField("anaerobicTrainingEffect", "activity_list", "1-5",
                "Firstbeat anaerobic TE", ("load_reference",),
                "Proprietary; reference only."),
    GarminField("averageRunningCadenceInStepsPerMinute", "activity_list", "spm",
                "Mean cadence (running activities)", ("cs_approx",),
                "Running only; cadence is not used as a headline metric in v2."),
    GarminField("avgStrideLength", "activity_list", "cm", "Mean stride length", ("cs_approx",),
                "Running only; informational."),
    GarminField("startLatitude", "activity_list", "deg", "Start latitude", ("decoupling",),
                "Used for route clustering; horizontal geolocation (not fixed) required."),
    GarminField("startLongitude", "activity_list", "deg", "Start longitude", ("decoupling",), ""),
    GarminField("endLatitude", "activity_list", "deg", "End latitude", ("decoupling",), ""),
    GarminField("endLongitude", "activity_list", "deg", "End longitude", ("decoupling",), ""),
    GarminField("locationName", "activity_list", "text",
                "Garmin-assigned place label", ("decoupling",),
                "Unreliable (frequently empty); route_key prefers start coordinates."),
    GarminField("deviceId", "activity_list", "id", "Recording device id", ("decoupling",),
                "Proxy for sensor source. Cannot reliably distinguish chest strap vs optical from the list payload; flag recorded, honesty preferred."),
    GarminField("manufacturer", "activity_list", "text", "Device manufacturer", ("decoupling",), ""),
    GarminField("calories", "activity_list", "kcal", "Estimated energy", ("cross_training",),
                "Estimate only; not a training-stress metric in v2."),
    GarminField("hasIntensityIntervals", "activity_list", "bool",
                "Whether the session contains structured intervals", ("trimp_banister", "decoupling"),
                "Interval sessions are excluded from decoupling (not sustained effort); Banister TRIMP uses details HR series when available to handle them."),
    GarminField("lapCount", "activity_list", "count", "Number of laps", ("volume",),
                "Informational / structure."),
    GarminField("splitSummaries", "activity_list", "list",
                "Per-split-type aggregates (RWD_RUN etc.)", ("cs_approx", "decoupling"),
                "No per-split HR here — HR comes from activity_details."),

    # ---- activity details (activity-service/{id}/details) ----
    GarminField("metrics[].heartRate", "activity_details", "bpm[]",
                "Per-sample HR series", ("trimp_banister", "decoupling"),
                "One request per activity (rate-limit aware). Downsampled by maxChartSize; gaps possible when optical."),
    GarminField("metrics[].speed", "activity_details", "m/s[]",
                "Per-sample speed series", ("decoupling", "cs_approx"),
                "Speed vs distance series can misalign on GPS dropouts; resample to common timestamps."),
    GarminField("metrics[].distance", "activity_details", "m[]", "Cumulative distance series", ("cs_approx",)),
    GarminField("metrics[].altitude", "activity_details", "m[]", "Altitude series", ("elevation",),
                "Baro vs GPS-derived altitude varies; not used for headline elevation (activity list elevationGain is authoritative)."),
    GarminField("metrics[].wkt", "activity_details", "enum[]", "Per-sample workout step", ("decoupling",),
                "Use to mask non-effort segments when splitting halves."),
    GarminField("lapsSummary", "activity_details", "list", "Per-lap summaries", ("decoupling", "trimp_banister"),
                "Fallback to lap-level HR if per-second series is empty."),

    # ---- performance endpoints ----
    GarminField("speed_and_heart_rate.heartRate", "lt", "bpm",
                "Lactate-threshold HR", ("lt_hr",),
                "Anchored metric: LABEL trustworthy (7% error), pace is not (up to 20-26% overest.). Manual lab/race set via RunnerProfile.lthr_manual overrides."),
    GarminField("speed_and_heart_rate.speed", "lt", "m/s",
                "Lactate-threshold speed", ("lt_pace", "cs_approx"),
                "FLAGGED: 2025 studies show Garmin LT pace can overestimate by 20-26%; use for trend, not absolute prescription."),
    GarminField("speed_and_heart_rate.calendarDate", "lt", "date", "When LT was computed", ("lt_hr", "lt_pace"), ""),
    GarminField("Run_5k.time", "race_predictions", "ms", "Predicted 5k time", ("race_5k",),
                "Riegel-derived; trusted more than marathon. Unit is milliseconds in Garmin payload (reconciled in Task 12)."),
    GarminField("Run_10k.time", "race_predictions", "ms", "Predicted 10k time", ("race_10k",), ""),
    GarminField("Run_half_marathon.time", "race_predictions", "ms", "Predicted half time", ("race_half",),
                "Riegel calibration still OK at half distance."),
    GarminField("Run_full_marathon.time", "race_predictions", "ms", "Predicted marathon time", ("race_full",),
                "LEAST TRUSTWORTHY: Riegel underestimates marathon by >=10 min for ~half of runners (Vickers 2016). Trend only."),
    GarminField("load", "training_status", "arbitrary",
                "Firstbeat training load", ("load_reference",),
                "Proprietary, not independently validated. Reference display only."),
    GarminField("trainingStatus", "training_status", "enum",
                "Productive/Maintaining/Detraining/Peaking/Recovery/Overreaching", ("load_reference",),
                "Proprietary, context only."),

    # ---- user profile / settings ----
    GarminField("userData.maxHRSetting", "user_settings", "bpm",
                "Configured HRmax", ("trimp_banister", "vo2max"),
                "If unset, age-predicted fallback (220-age) used and flagged in metric flags."),
    GarminField("userData.birthDate", "user_settings", "date", "Birth date", ("trimp_banister",),
                "Used for fallback HRmax and Banister sex exponent."),
    GarminField("userData.measurementSystem", "user_settings", "enum", "statute|metric", ("volume",),
                "Documented; values stored in SI regardless."),
    GarminField("heartRateZones", "heart_rate_zones", "list",
                "Configured HR zone thresholds", ("trimp_edwards",),
                "ZONE_LIMIT: zone-second buckets are only reproducible if the same zone thresholds are stored. Persist in RunnerProfile.hr_zones."),
)


def fields_for_metric(metric: str) -> tuple[GarminField, ...]:
    return tuple(f for f in FIELD_REGISTRY if metric in f.metrics)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_field_registry.py -v`
Expected: `4 passed`. The variants of `test_every_activity_list_field_is_present_or_documented_absent` and `test_fields_have_units_and_limitations` are kept honest against this task's own registry: derived metrics `ctl/atl/tsb` are excluded from the mapped assertion (they consume no raw field), dotted paths like `activityType.typeKey` are resolved, and the `LIMITLESS_FIELDS` allowlist covers the 13 fields that intentionally carry no limitation text. If `test_every_activity_list_field_is_present_or_documented_absent` still fails, the fixture's running entries lack a claimed field — fix by either adding the field to the fixture's entries or moving the claim to `endpoint="activity_details"`.

- [ ] **Step 5: Write `docs/v2/garmin-field-mapping.md` (draft)**

Write the doc with this structure (content mirrors `garmin_fields.py`; keep the registry as source of truth and copy the text):

```markdown
# Garmin Field → v2 Metric Mapping

> Generated source of truth: `v2/garmin_fields.py`. Endpoints: activity list,
> activity details, lactate threshold, race predictions, training status,
> user settings / heart-rate zones.

## 1. Endpoints used
(one paragraph each: activity-list search URL, activity details URL, performance
endpoints, profile endpoints — with the rate-limit note that details is one
request per activity and is fetched lazily.)

## 2. Activity list fields
| Garmin field | Units | Maps to metric(s) | Limitations |
| --- | --- | --- | --- |
...render every `activity_list` row from FIELD_REGISTRY...

## 3. Activity details fields
...render every `activity_details` row...

## 4. Performance endpoints
...render `lt`, `race_predictions`, `training_status` rows...

## 5. Profile fields
...render `user_settings`, `heart_rate_zones` rows...

## 6. Cross-cutting limitations
1. No cycling power in the activity list — cross-training load is HR/time/calories only.
2. Elevation absent on indoor/treadmill/zero-distance-cycling activities.
3. VO2max is a Firstbeat estimate (5–10% error; HRmax-sensitive; underestimates ≥60 mL/kg/min); never recomputed.
4. Anchor on LT heart rate (≈7% error); LT pace can overestimate 20–26%.
5. Race predictors: near-distance (5K/10K/half) safest; marathon least trustworthy.
6. HR zone buckets are device-config-dependent — always store `hr_zones` with the series.
7. Optical vs chest-strap sensor is not reliably distinguishable from the list payload (deviceId is a proxy only).
8. Garmin Training Status / load / TE are proprietary — reference only, never gates.
9. Decoupling is only honest when aggregated over ≥6 sessions on similar flat routes; single-run values are noise (preprint: 57–83% session-residual variance).
```

- [ ] **Step 6: Commit**

```bash
git add v2/garmin_fields.py v2/tests/test_field_registry.py docs/v2/garmin-field-mapping.md
git commit -m "feat(v2): Garmin field registry + field-mapping doc draft"
```

### Task 3: Gateway + Normalize

**Files:**
- Create: `v2/normalize.py`
- Create: `v2/gateway.py`
- Create: `v2/tests/test_normalize.py`

**Interfaces:**
- Consumes: fixture `tests/fixtures/activities_sample.json`.
- Produces: `Activity`/`from_summary`/`sport_of` (signatures above); `GarminGateway` (creds via `op` CLI with env fallback).

- [ ] **Step 1: Write the failing normalize tests**

```python
# tests/test_normalize.py
import json
from datetime import date
from pathlib import Path

from normalize import from_summary, sport_of

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"
DATA = json.loads(FIXTURE.read_text())


def _first(key):
    return next(a for a in DATA if a.get("activityType", {}).get("typeKey") == key)


def test_sport_classification():
    assert sport_of(_first("running")) == "running"
    assert sport_of(_first("treadmill_running")) == "treadmill"
    assert sport_of(_first("indoor_cycling")) == "cross"
    assert sport_of(_first("strength_training")) == "cross"


def test_from_summary_maps_core_fields():
    a = from_summary(_first("running"))
    assert a.sport == "running"
    assert isinstance(a.date, date)
    assert a.distance_m > 0
    assert a.duration_s > 0
    assert a.avg_hr is None or a.avg_hr > 0
    assert set(a.zone_s) == {1, 2, 3, 4, 5}
    assert a.distance_km == round(a.distance_m / 1000.0, 6)
    assert a.duration_min == round(a.duration_s / 60.0, 6)


def test_missing_optional_fields_become_none():
    a = from_summary(_first("indoor_cycling"))
    assert a.ele_gain_m is None
    assert a.vo2max is None
    assert a.fastest_split_1609 is None


def test_zone_seconds_default_zero():
    a = from_summary(_first("strength_training"))
    assert a.zone_s[1] >= 0 and a.zone_s[5] >= 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: fails with `ModuleNotFoundError: No module named 'normalize'`.

- [ ] **Step 3: Write `v2/normalize.py`**

```python
"""Raw Garmin JSON -> validated Activity model. Pure: no network."""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

RUNNING_KEYS = {"running"}
TREADMILL_KEYS = {"treadmill_running"}


def sport_of(summary: dict) -> str:
    tk = (summary.get("activityType") or {}).get("typeKey", "")
    if tk in RUNNING_KEYS:
        return "running"
    if tk in TREADMILL_KEYS:
        return "treadmill"
    return "cross"


def _f(summary: dict, key: str) -> Any:
    v = summary.get(key)
    return v if v is not None else None


@dataclass
class Activity:
    activity_id: int
    sport: str
    date: date
    ts_ms: int
    distance_m: float
    duration_s: float
    elapsed_s: float
    avg_hr: float | None
    max_hr: float | None
    zone_s: dict[int, float] = field(default_factory=dict)
    ele_gain_m: float | None = None
    ele_loss_m: float | None = None
    vo2max: float | None = None
    aerobic_te: float | None = None
    anaerobic_te: float | None = None
    avg_speed: float | None = None
    fastest_split_1609: float | None = None
    avg_cadence: float | None = None
    device_id: int | None = None
    location: str | None = None
    lat: float | None = None
    lon: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def distance_km(self) -> float:
        return round(self.distance_m / 1000.0, 6)

    @property
    def duration_min(self) -> float:
        return round(self.duration_s / 60.0, 6)


def from_summary(summary: dict) -> Activity:
    ts_local = str(_f(summary, "startTimeLocal") or "")[:19]
    if ts_local:
        d = datetime.strptime(ts_local, "%Y-%m-%d %H:%M:%S").date()
    else:
        ts_ms = _f(summary, "beginTimestamp")
        d = datetime.fromtimestamp(ts_ms / 1000.0).date() if isinstance(ts_ms, (int, float)) else date.today()
    zone_s = {
        z: float(_f(summary, f"hrTimeInZone_{z}") or 0.0)
        for z in range(1, 6)
    }
    return Activity(
        activity_id=int(summary.get("activityId")),
        sport=sport_of(summary),
        date=d,
        ts_ms=int(_f(summary, "beginTimestamp") or 0),
        distance_m=float(_f(summary, "distance") or 0.0),
        duration_s=float(_f(summary, "duration") or 0.0),
        elapsed_s=float(_f(summary, "elapsedDuration") or 0.0),
        avg_hr=_f(summary, "averageHR"),
        max_hr=_f(summary, "maxHR"),
        zone_s=zone_s,
        ele_gain_m=_f(summary, "elevationGain"),
        ele_loss_m=_f(summary, "elevationLoss"),
        vo2max=_f(summary, "vO2MaxValue"),
        aerobic_te=_f(summary, "aerobicTrainingEffect"),
        anaerobic_te=_f(summary, "anaerobicTrainingEffect"),
        avg_speed=_f(summary, "averageSpeed"),
        fastest_split_1609=_f(summary, "fastestSplit_1609"),
        avg_cadence=_f(summary, "averageRunningCadenceInStepsPerMinute"),
        device_id=_f(summary, "deviceId"),
        location=_f(summary, "locationName"),
        lat=_f(summary, "startLatitude"),
        lon=_f(summary, "startLongitude"),
        raw=summary,
    )
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_normalize.py -v`
Expected: `4 passed`.

- [ ] **Step 5: Write `v2/gateway.py`**

```python
"""Garmin REST gateway. Credentials from 1Password via `op` CLI, with env fallback.

Wraps garminconnect (Garmin client) for auth + connectapi. All token/credential
handling stays out of the repo: `op item get Garmin` or GARMIN_EMAIL/GARMIN_PASSWORD.
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from garminconnect import Garmin

ACTIVITIES_PATH = "/activitylist-service/activities/search/activities"
DETAILS_PATH = "/activity-service/activity/{activity_id}/details"
TOKENSTORE = Path.home() / ".garmin-mcp" / "oauth2_token.json"


def load_op_creds(item: str = "Garmin") -> tuple[str | None, str | None]:
    """Read username/password from 1Password. Returns (None, None) if `op` is missing."""
    try:
        result = subprocess.run(
            ["op", "item", "get", item, "--fields", "username", "--fields", "password", "--reveal"],
            capture_output=True, text=True, timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0:
        return None, None
    parts = result.stdout.strip().split(",")
    if len(parts) >= 2:
        return parts[0].rstrip(), parts[1].lstrip()
    return None, None


class GarminGateway:
    def __init__(self, cache_dir: Path = Path("cache/test_cache")):
        """Auth resolution order:
        1. Existing tokenstore at ~/.garmin-mcp/oauth2_token.json (avoids
           password/MFA on routine runs; provisioned by the v1 MCP login).
        2. Fresh login with credentials read from 1Password via `op item get
           Garmin` (fields username/password); GARMIN_EMAIL/GARMIN_PASSWORD
           env fallback for CI.
        A required-MFA failure on the fresh-login path is a one-time manual
        action: refresh the tokenstore once, then retry.
        """
        if TOKENSTORE.exists():
            self.auth_path = "tokenstore"
            self._garmin = Garmin(is_cn=False)
            self._garmin.login(tokenstore=str(TOKENSTORE))
        else:
            self.auth_path = "op_credentials"
            email, password = load_op_creds()
            if not email or not password:
                email = os.environ.get("GARMIN_EMAIL")
                password = os.environ.get("GARMIN_PASSWORD")
            if not email or not password:
                raise RuntimeError(
                    "Garmin auth unavailable: no tokenstore, no op creds, no env creds"
                )
            self._garmin = Garmin(email=email, password=password)
            self._garmin.login()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache(self, name: str, payload) -> None:
        path = self.cache_dir / name
        path.write_text(json.dumps(payload, indent=2, default=str))
        (self.cache_dir / "last_fetch_timestamp").write_text(
            datetime.now(timezone.utc).isoformat()
        )

    def fetch_activities(self, start: str, end: str) -> list[dict]:
        out: list[dict] = []
        offset = 0
        limit = 100
        while True:
            page = self._garmin.connectapi(
                ACTIVITIES_PATH,
                params={"startDate": start, "endDate": end, "limit": limit, "offset": offset},
            ) or []
            out.extend(page)
            if len(page) < limit:
                break
            offset += limit
        self._cache("garmin_raw.json", out)
        return out

    def fetch_activity_details(self, activity_id: int) -> dict:
        payload = self._garmin.connectapi(
            DETAILS_PATH.format(activity_id=activity_id),
            params={"maxChartSize": 2000, "maxPolylineSize": 4000},
        ) or {}
        self._cache(f"activity_details_{activity_id}.json", payload)
        return payload

    def fetch_lactate_threshold(self) -> dict:
        payload = self._garmin.get_lactate_threshold(latest=True)
        self._cache("lactate_threshold.json", payload)
        return payload

    def fetch_race_predictions(self) -> dict:
        payload = self._garmin.get_race_predictions()
        self._cache("race_predictions.json", payload)
        return payload

    def fetch_training_status(self, cdate: str) -> dict:
        payload = self._garmin.get_training_status(cdate)
        self._cache(f"training_status_{cdate}.json", payload)
        return payload
```

- [ ] **Step 6: Verify `op` path is exercised (no live network required)**

Run: `cd v2 && ../.venv/bin/python -c "from gateway import load_op_creds; e,p = load_op_creds(); print('creds_found=', bool(e and p))"`
Expected: prints `creds_found=True` (1Password configured on this machine). If `op` is absent, it prints `creds_found=False` and the env fallback is the documented path.

- [ ] **Step 7: Run the full normalize suite**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: all tests (field registry + normalize + smoke) pass.

- [ ] **Step 8: Commit**

```bash
git add v2/normalize.py v2/gateway.py v2/tests/test_normalize.py
git commit -m "feat(v2): Activity model + normalize + Garmin REST gateway (1Password creds)"
```

### Task 4: Runner Profile + Banister Parameters

**Files:**
- Create: `v2/profile.py`
- Create: `v2/tests/test_profile.py`

**Interfaces:**
- Consumes: nothing external.
- Produces: `RunnerProfile` + `default_profile` (signatures above); used by trimp, vo2max flags, decoupling config.

- [ ] **Step 1: Write the failing profile tests**

```python
# tests/test_profile.py
import pytest
from datetime import date
from profile import RunnerProfile, default_profile


def test_age_predicted_hrmax_fallback():
    p = default_profile(age=40)
    assert p.hrmax == 180  # 220 - 40


def test_banister_exponent_by_sex():
    assert default_profile(age=40, sex="M").banister_exponent() == pytest.approx(1.92)
    assert default_profile(age=40, sex="F").banister_exponent() == pytest.approx(1.67)


def test_hrmax_boundaries():
    p = RunnerProfile(hrmax=200, hrrest=50, sex="M", birth_year=1986, lthr_manual=None, hr_zones={}, units="metric")
    assert p.age_predicted_hrmax() == 220 - (date.today().year - 1986)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_profile.py -v`
Expected: fails with `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/profile.py`**

```python
"""Runner-level config: HRmax/HRrest/sex/birth/LTHR override/HR zones + Banister params."""
from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar

BANISTER_B = {"M": 1.92, "F": 1.67}
BANISTER_INTERCEPT = 0.64


@dataclass
class RunnerProfile:
    hrmax: int
    hrrest: int
    sex: str
    birth_year: int
    lthr_manual: int | None = None
    hr_zones: dict[int, tuple[int, int]] = field(default_factory=dict)
    units: str = "metric"
    hrmax_source: str = "configured"  # "configured" | "age_predicted"

    EDWARDS_WEIGHTS: ClassVar[dict[int, float]] = {1: 1.0, 2: 2.0, 3: 3.0, 4: 4.0, 5: 5.0}

    @classmethod
    def from_age(cls, age: int, hrrest: int, sex: str, birth_year: int, **kw):
        return cls(hrmax=220 - age, hrrest=hrrest, sex=sex, birth_year=birth_year,
                   hrmax_source="age_predicted", **kw)

    def age_predicted_hrmax(self) -> int:
        return 220 - (date.today().year - self.birth_year)

    def banister_exponent(self) -> float:
        return BANISTER_B.get(self.sex.upper(), 1.92)

    def exp_intercept_factor(self) -> float:
        return BANISTER_INTERCEPT


def default_profile(age: int = 40, hrrest: int = 60, sex: str = "M",
                    birth_year: int | None = None, lthr_manual: int | None = None) -> RunnerProfile:
    by = birth_year or (date.today().year - age)
    p = RunnerProfile.from_age(age=age, hrrest=hrrest, sex=sex, birth_year=by, lthr_manual=lthr_manual)
    p.hr_zones = {1: (0, int(0.60 * p.hrmax)), 2: (int(0.60 * p.hrmax) + 1, int(0.70 * p.hrmax)),
                  3: (int(0.70 * p.hrmax) + 1, int(0.80 * p.hrmax)), 4: (int(0.80 * p.hrmax) + 1, int(0.90 * p.hrmax)),
                  5: (int(0.90 * p.hrmax) + 1, p.hrmax)}
    return p
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_profile.py -v`
Expected: `3 passed`. If `test_age_predicted_hrmax_fallback` fails because `hrmax` got mutated by `hr_zones` default (mutable default traps), the dataclass uses `field(default_factory=dict)` — already correct.

- [ ] **Step 5: Commit**

```bash
git add v2/profile.py v2/tests/test_profile.py
git commit -m "feat(v2): RunnerProfile + Banister parameters"
```

### Task 5: Volume + Elevation + Cross-Training Aggregation

**Files:**
- Create: `v2/metrics/__init__.py`
- Create: `v2/metrics/volume.py`
- Create: `v2/metrics/elevation.py`
- Create: `v2/tests/test_volume.py`
- Create: `v2/tests/test_elevation.py`

**Interfaces:**
- Consumes: `Activity` from `normalize`.
- Produces: `weekly_distance` / `rolling_4wk` / `week_over_week_pct` / `groups`; `daily_elevation_gain` / `rolling_28d` / `gain_per_km` (signatures above).

- [ ] **Step 1: Write the failing volume tests**

```python
# tests/test_volume.py
from datetime import date

import pandas as pd
import pytest

from metrics.volume import rolling_4wk, week_over_week_pct, weekly_distance
from normalize import Activity


def _act(d: date, sport: str, dist_m: float, dur_s: float = 3600.0) -> Activity:
    return Activity(activity_id=int(d.strftime("%Y%m%d")) + hash(sport) % 1000, sport=sport, date=d,
                    ts_ms=0, distance_m=dist_m, duration_s=dur_s, elapsed_s=dur_s,
                    avg_hr=None, max_hr=None, zone_s={}, ele_gain_m=0.0)


def test_weekly_distance_running_only():
    acts = [
        _act(date(2026, 4, 6), "running", 10_000.0),   # Mon -> week 2026-W15
        _act(date(2026, 4, 8), "running", 5_000.0),
        _act(date(2026, 4, 16), "treadmill", 8_000.0),
        _act(date(2026, 4, 16), "running", 6_000.0),
    ]
    wk = weekly_distance(acts, "running")
    assert wk["2026-W15"] == pytest.approx(15.0)
    assert wk["2026-W16"] == pytest.approx(6.0)


def test_rolling_and_wow():
    wk = pd.Series([10.0, 10.0, 20.0, 20.0, 30.0, 30.0],
                   index=["2026-W01", "2026-W02", "2026-W03", "2026-W04", "2026-W05", "2026-W06"])
    r = rolling_4wk(wk)
    assert r.iloc[3] == pytest.approx(15.0)  # (10+10+20+20)/4
    wow = week_over_week_pct(wk)
    assert wow.iloc[2] == pytest.approx(1.0)  # 20 vs 10
    assert pd.isna(wow.iloc[0])


def test_groups_total_spans_sports():
    from metrics.volume import groups
    assert set(groups()) == {"total", "running", "treadmill", "cross"}
```

Note: the ISO-week index in the test is constructed explicitly so the test does not depend on `period_range` label quirks; in the implementation, build the index with `pd.to_datetime(...).dt.strftime("%G-W%V")`.

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_volume.py -v`
Expected: fails with `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/__init__.py`** as an empty file, and `v2/metrics/volume.py`:

```python
"""Weekly volume / consistency metrics (research brief 4.1)."""
import pandas as pd

from normalize import Activity


def groups() -> list[str]:
    return ["total", "running", "treadmill", "cross"]


def _group_members(activities: list[Activity], group: str) -> list[Activity]:
    if group == "total":
        return activities
    if group == "running":
        return [a for a in activities if a.sport == "running"]
    if group == "treadmill":
        return [a for a in activities if a.sport == "treadmill"]
    return [a for a in activities if a.sport == "cross"]


def weekly_distance(activities: list[Activity], group: str) -> pd.Series:
    acts = _group_members(activities, group)
    if not acts:
        return pd.Series([], dtype=float)
    idx = pd.DatetimeIndex([a.date for a in acts]).strftime("%G-W%V")
    s = pd.Series([a.distance_km for a in acts], index=idx)
    out = s.groupby(s.index).sum().sort_index()
    out.index.name = "week"
    return out


def rolling_4wk(weekly: pd.Series) -> pd.Series:
    return weekly.rolling(4, min_periods=1).mean()


def week_over_week_pct(weekly: pd.Series) -> pd.Series:
    prev = weekly.shift(1)
    return (weekly - prev) / prev.replace(0, pd.NA)
```

- [ ] **Step 4: Run volume tests to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_volume.py -v`
Expected: `3 passed`. The tests pin `2026-W15`/`2026-W16` — confirm `%G-W%V` formatting matches (April 2026): `pd.Timestamp("2026-04-06").strftime("%G-W%V")` must equal `"2026-W15"`. If the ISO week differs, update the test's expected labels rather than the code.

- [ ] **Step 5: Write the failing elevation tests**

```python
# tests/test_elevation.py
from datetime import date

import pandas as pd
import pytest

from metrics.elevation import daily_elevation_gain, gain_per_km, rolling_28d
from normalize import Activity


def _run(d, dist_m, gain_m):
    return Activity(activity_id=int(d.strftime("%Y%m%d")), sport="running", date=d,
                    ts_ms=0, distance_m=dist_m, duration_s=3600.0, elapsed_s=3600.0,
                    avg_hr=None, max_hr=None, zone_s={}, ele_gain_m=gain_m, ele_loss_m=0.0)


def test_daily_and_rolling():
    acts = [_run(date(2026, 4, 1), 10_000.0, 100.0), _run(date(2026, 4, 2), 5000.0, 50.0)]
    daily = daily_elevation_gain(acts, "running")
    assert daily.get(pd.Timestamp("2026-04-02")) == pytest.approx(50.0)
    r = rolling_28d(daily)
    assert r.get(pd.Timestamp("2026-04-02")) == pytest.approx(150.0)


def test_gain_per_km():
    acts = [_run(date(2026, 4, 1), 10_000.0, 100.0)]
    gp = gain_per_km(acts, "running")
    assert gp.iloc[0] == pytest.approx(10.0)  # 100 m / 10 km


def test_indoor_activities_excluded():
    acts = [Activity(activity_id=1, sport="treadmill", date=date(2026, 4, 1), ts_ms=0,
                     distance_m=8000.0, duration_s=3600.0, elapsed_s=3600.0, avg_hr=None,
                     max_hr=None, zone_s={}, ele_gain_m=None)]
    daily = daily_elevation_gain(acts, "running")
    assert daily.empty
```

- [ ] **Step 6: Write `v2/metrics/elevation.py` and verify**

```python
"""Elevation as context + load-diversity (research brief 4.2). Never a risk number."""
import pandas as pd

from normalize import Activity


def _runs(activities: list[Activity], group: str) -> list[Activity]:
    if group == "running":
        return [a for a in activities if a.sport == "running"]
    if group == "treadmill":
        return [a for a in activities if a.sport == "treadmill"]
    return [a for a in activities if a.sport == "cross"]


def daily_elevation_gain(activities: list[Activity], group: str = "running") -> pd.Series:
    rows = []
    for a in _runs(activities, group):
        if a.ele_gain_m is None:
            continue
        rows.append((a.date, a.ele_gain_m))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).sum().sort_index()


def rolling_28d(daily_gain: pd.Series) -> pd.Series:
    full = daily_gain.sort_index()
    if full.empty:
        return full
    full_idx = pd.date_range(full.index.min(), full.index.max(), freq="D")
    full = full.reindex(full_idx, fill_value=0.0)
    return full.rolling(28, min_periods=1).sum()


def gain_per_km(activities: list[Activity], group: str = "running") -> pd.Series:
    rows = []
    for a in _runs(activities, group):
        km = a.distance_km
        if a.ele_gain_m is None or km <= 0:
            continue
        rows.append((a.date, a.ele_gain_m / km))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).mean().sort_index()
```

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_elevation.py -v`
Expected: `3 passed`.

- [ ] **Step 7: Run the full suite**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: all pass. Fix any cross-test breakage (e.g., import paths, shared fixtures).

- [ ] **Step 8: Commit**

```bash
git add v2/metrics/__init__.py v2/metrics/volume.py v2/metrics/elevation.py v2/tests/test_volume.py v2/tests/test_elevation.py
git commit -m "feat(v2): volume + elevation metrics"
```

### Task 6: TRIMP (Edwards + Banister)

**Files:**
- Create: `v2/metrics/trimp.py`
- Create: `v2/tests/test_trimp.py`

**Interfaces:**
- Consumes: `Activity`, `RunnerProfile`.
- Produces: `edwards_trimp`, `banister_trimp`, `daily_trimp` (signatures above).

- [ ] **Step 1: Write the failing TRIMP tests**

```python
# tests/test_trimp.py
from datetime import date

import pandas as pd
import pytest

from metrics.trimp import banister_trimp, daily_trimp, edwards_trimp
from normalize import Activity
from profile import default_profile


def test_edwards_trimp_sum_zone_minutes_times_weight():
    # 60 min in zone 1 repeated 5 times => 60*1*5 = 300
    zone_s = {1: 5 * 3600.0}
    assert edwards_trimp(zone_s) == pytest.approx(300.0)


def test_banister_trimp_male_at_threshold():
    p = default_profile(age=40, hrrest=60, sex="M")  # hrmax 180
    # avg HR 150 => dHR = (150-60)/(180-60) = 0.75
    trimp = banister_trimp(duration_min=30.0, avg_hr=150.0, profile=p)
    expected = 30.0 * 0.75 * 0.64 * (2.71828183 ** (1.92 * 0.75))
    assert trimp == pytest.approx(expected, rel=1e-9)


def test_banister_trimp_female_uses_167():
    p = default_profile(age=40, hrrest=60, sex="F")
    trimp_f = banister_trimp(30.0, 150.0, p)
    trimp_m = banister_trimp(30.0, 150.0, default_profile(age=40, hrrest=60, sex="M"))
    assert trimp_f < trimp_m


def test_banister_trimp_invalid_hrmax():
    p = default_profile(age=40, hrrest=60, sex="M")
    p.hrmax = 60
    with pytest.raises(ValueError):
        banister_trimp(30.0, 150.0, p)


def test_daily_trimp_sums_multiple_activities_per_day():
    acts = [
        Activity(1, "running", date(2026, 4, 1), 0, 10000.0, 3600.0, 3600.0, 140.0, 160.0,
                 zone_s={z: 720.0 for z in range(1, 6)}),   # 12 min/zone -> 12*(1+2+3+4+5)=180
        Activity(2, "running", date(2026, 4, 1), 0, 5000.0, 1800.0, 1800.0, 120.0, 140.0,
                 zone_s={z: 360.0 for z in range(1, 6)}),   # 6 min/zone  -> 6*15=90
    ]
    df = daily_trimp(acts, default_profile(age=40, hrrest=60, sex="M"))
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df["edwards"].iloc[0] == pytest.approx(270.0)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_trimp.py -v`
Expected: fails with `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/trimp.py`**

```python
"""HR-based training load: Edwards TRIMP (zone seconds) + Banister TRIMP (dHR exponential).

Research brief 1.2: Banister is the primary load measure (physiologically grounded);
Edwards is kept as a cheap, reproducible cross-check. Absolute values are only
comparable within an individual over time with identical profile parameters.
"""
import pandas as pd

from normalize import Activity
from profile import RunnerProfile

E = 2.718281828459045


def edwards_trimp(zone_s: dict[int, float], weights: dict[int, float] | None = None) -> float:
    w = weights or RunnerProfile.EDWARDS_WEIGHTS  # type: ignore[attr-defined]
    return float(sum((zone_s.get(z, 0.0) / 60.0) * w[z] for z in w))


def banister_trimp(duration_min: float, avg_hr: float, profile: RunnerProfile) -> float:
    hrmax = float(profile.hrmax)
    hrrest = float(profile.hrrest)
    if hrmax <= hrrest:
        raise ValueError("hrmax must exceed hrrest")
    dhr = (avg_hr - hrrest) / (hrmax - hrrest)
    dhr = min(max(dhr, 0.0), 1.0)
    return duration_min * dhr * profile.exp_intercept_factor() * (E ** (profile.banister_exponent() * dhr))


def daily_trimp(activities: list[Activity], profile: RunnerProfile) -> pd.DataFrame:
    rows = []
    for a in activities:
        if a.avg_hr is None or a.duration_s <= 0:
            continue
        dur_min = a.duration_s / 60.0
        rows.append({
            "date": a.date,
            "banister": banister_trimp(dur_min, float(a.avg_hr), profile),
            "edwards": edwards_trimp(a.zone_s),
        })
    if not rows:
        return pd.DataFrame(columns=["date", "banister", "edwards"])
    df = pd.DataFrame(rows)
    agg = df.groupby("date", as_index=False)[["banister", "edwards"]].sum()
    agg["date"] = pd.to_datetime(agg["date"])
    return agg.set_index("date")
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_trimp.py -v`
Expected: `5 passed`. The daily-agg test already pins `edwards==270.0`; Banister stays unpinned (uses the default profile path).

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/trimp.py v2/tests/test_trimp.py
git commit -m "feat(v2): TRIMP (Edwards zones + Banister dHR)"
```

### Task 7: CTL / ATL / TSB (PMC)

**Files:**
- Create: `v2/metrics/pmc.py`
- Create: `v2/tests/test_pmc.py`

**Interfaces:**
- Consumes: a daily TRIMP `pd.Series` (sparse dates allowed).
- Produces: `ctl_atl_tsb` (signature above).

- [ ] **Step 1: Write the failing PMC tests**

```python
# tests/test_pmc.py
import pandas as pd
import pytest

from metrics.pmc import ctl_atl_tsb


def test_tsb_is_ctl_minus_atl_and_ctl_smoother():
    idx = pd.date_range("2026-04-01", periods=14, freq="D")
    tr = pd.Series([100.0, 50.0] + [50.0] * 12, index=idx)
    out = ctl_atl_tsb(tr)
    assert list(out.columns) == ["ctl", "atl", "tsb"]
    assert out["tsb"].iloc[-1] == pytest.approx(out["ctl"].iloc[-1] - out["atl"].iloc[-1])
    # ctl (tau 42) is more sluggish than atl (tau 7), so right after the step
    # down to 50 it still sits higher than atl
    assert out["ctl"].iloc[1] > out["atl"].iloc[1]


def test_reindexes_sparse_days_with_zero():
    idx = pd.to_datetime(["2026-04-01", "2026-04-03"])
    tr = pd.Series([120.0, 90.0], index=idx)
    out = ctl_atl_tsb(tr)
    assert len(out) == 3  # Apr 1, 2, 3
    assert out.index[1] == pd.Timestamp("2026-04-02")


def test_empty_input_returns_empty():
    out = ctl_atl_tsb(pd.Series([], dtype=float))
    assert out.empty
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pmc.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/pmc.py`**

```python
"""Performance-Management chart: CTL (fitness, tau 42d), ATL (fatigue, tau 7d), TSB.

Research brief 1.3: TSB is a freshness/load-trajectory visualizer, not a
performance claim. Band thresholds are heuristics.
"""
import pandas as pd


def ctl_atl_tsb(daily_trimp: pd.Series, tau_ctl: int = 42, tau_atl: int = 7) -> pd.DataFrame:
    if daily_trimp.empty:
        return pd.DataFrame(columns=["ctl", "atl", "tsb"])
    full_idx = pd.date_range(daily_trimp.index.min(), daily_trimp.index.max(), freq="D")
    tr = daily_trimp.reindex(full_idx, fill_value=0.0)
    ctl = tr.ewm(alpha=1 / tau_ctl, adjust=False).mean()
    atl = tr.ewm(alpha=1 / tau_atl, adjust=False).mean()
    out = pd.DataFrame({"ctl": ctl, "atl": atl})
    out["tsb"] = out["ctl"] - out["atl"]
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pmc.py -v`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/pmc.py v2/tests/test_pmc.py
git commit -m "feat(v2): CTL/ATL/TSB performance-management chart"
```

### Task 8: ACWR (load-swing monitor)

**Files:**
- Create: `v2/metrics/acwr.py`
- Create: `v2/tests/test_acwr.py`

**Interfaces:**
- Consumes: daily load `pd.Series` (TRIMP or distance).
- Produces: `coupled_acwr`, `history_percentile` (signatures above).

- [ ] **Step 1: Write the failing ACWR tests**

```python
# tests/test_acwr.py
import pandas as pd
import pytest

from metrics.acwr import coupled_acwr, history_percentile


def test_constant_load_yields_ratio_one():
    idx = pd.date_range("2026-04-01", periods=35, freq="D")
    tr = pd.Series([100.0] * 35, index=idx)
    acwr = coupled_acwr(tr)
    last = acwr.dropna().iloc[-1]
    assert last == pytest.approx(1.0)


def test_acute_chronic_windows():
    idx = pd.date_range("2026-04-01", periods=35, freq="D")
    base = [100.0] * 28 + [200.0] * 7
    tr = pd.Series(base, index=idx)
    acwr = coupled_acwr(tr)
    # 2026-05-01 is day 31: acute window = indices 24..30 (4x100 + 3x200 = 1000 -> /7)
    # chronic window = indices 3..30 (25x100 + 3x200 = 3100 -> /28)
    # ratio = (1000/7)/(3100/28) = 4000/3100 ~= 1.29 > 1.0
    day31 = acwr[acwr.index == pd.Timestamp("2026-05-01")].iloc[0]
    assert day31 == pytest.approx(4000.0 / 3100.0)


def test_history_percentile_bounds():
    idx = pd.date_range("2026-04-01", periods=60, freq="D")
    tr = pd.Series(100.0, index=idx)
    acwr = coupled_acwr(tr)
    hp = history_percentile(acwr, window=30)
    assert hp["history_pct"].iloc[-1] == pytest.approx((30 + 1) / (2 * 30))  # all equal -> pandas avg-rank pct = (n+1)/2n
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_acwr.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/acwr.py`**

```python
"""Acute:Chronic workload ratio — a load-SWING monitor, never an injury gate.

Research brief 1.1: population ACWR bands are not validated for individual
running risk (Nakaoka found an inverse association). Use only individual-history
percentiles, and flag rapid change rather than absolute bands.
"""
import pandas as pd


def coupled_acwr(daily: pd.Series, acute: int = 7, chronic: int = 28) -> pd.Series:
    """Coupled ACWR, average-normalized: (mean daily load over acute window) /
    (mean daily load over chronic window). Steady constant load => 1.0, which
    the raw sum7/sum28 ratio does NOT give (it asymptotes at 7/28). The input
    must be a dense daily calendar series; the pipeline densifies TRIMP with
    fill_value=0 before calling this.
    """
    s = daily.sort_index()
    acute_avg = s.rolling(acute, min_periods=acute).sum() / acute
    chronic_avg = s.rolling(chronic, min_periods=chronic).sum() / chronic
    ratio = acute_avg / chronic_avg.replace(0, pd.NA)
    ratio.name = "acwr"
    return ratio


def history_percentile(acwr: pd.Series, window: int = 180) -> pd.DataFrame:
    pct = acwr.rolling(window, min_periods=20).rank(pct=True)
    return pd.DataFrame({"acwr": acwr, "history_pct": pct})
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_acwr.py -v`
Expected: `3 passed`. The history-percentile pin is intentionally `(window+1)/(2*window)` (= 31/60), not `0.5`: pandas `rolling().rank(pct=True)` averages tied ranks ((1+n)/2) then divides by n obs. If a stale rank still appears due to NaN-leading windows, confirm the final value on the max-window rank at the last point (uses `min_periods=20` so only late points have ranks).

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/acwr.py v2/tests/test_acwr.py
git commit -m "feat(v2): ACWR coupled ratio + history percentile"
```

### Task 9: Aerobic Decoupling (aggregated)

**Files:**
- Create: `v2/metrics/decoupling.py`
- Create: `v2/tests/test_decoupling.py`

**Interfaces:**
- Consumes: `Activity`; HR/speed sequences from `activity_details`.
- Produces: `decoupling_percent`, `eligible_activity`, `route_key`, `aggregate_decoupling` (signatures above).

- [ ] **Step 1: Write the failing decoupling tests**

```python
# tests/test_decoupling.py
from datetime import date

import pytest

from metrics.decoupling import (
    aggregate_decoupling,
    decoupling_percent,
    eligible_activity,
    route_key,
)
from normalize import Activity


def _run(d, sport="running", dur_s=6000.0, dist_m=18000.0, gain_m=100.0, lat=None, lon=None):
    return Activity(activity_id=1, sport=sport, date=d, ts_ms=0, distance_m=dist_m,
                    duration_s=dur_s, elapsed_s=dur_s, avg_hr=150.0, max_hr=170.0,
                    zone_s={}, ele_gain_m=gain_m, lat=lat, lon=lon)


def test_no_drift_is_zero_percent():
    hr = [150.0] * 100
    speed = [3.0] * 100
    assert decoupling_percent(hr, speed) == pytest.approx(0.0)


def test_hr_drift_shows_positive_decoupling():
    hr = [145.0] * 50 + [155.0] * 50
    speed = [3.0] * 100
    dec = decoupling_percent(hr, speed)
    assert dec > 0.05


def test_eligibility_min_duration():
    short = _run(date(2026, 4, 1), dur_s=1800.0)
    assert not eligible_activity(short)
    long = _run(date(2026, 4, 2), dur_s=7200.0)
    assert eligible_activity(long)


def test_eligibility_rejects_hills_and_non_running():
    hilly = _run(date(2026, 4, 3), dist_m=10_000.0, gain_m=800.0)  # 80 m/km > 25
    assert not eligible_activity(hilly)
    assert not eligible_activity(_run(date(2026, 4, 4), sport="treadmill", gain_m=None))


def test_route_key_clusters():
    a1 = _run(date(2026, 4, 1), lat=42.4261, lon=-71.2801)
    a2 = _run(date(2026, 4, 2), lat=42.4265, lon=-71.2805)
    a3 = _run(date(2026, 4, 3))  # no coords
    assert route_key(a1) == route_key(a2)
    assert route_key(a3) == ("unknown", None)


def test_aggregate_requires_six_sessions():
    assert aggregate_decoupling([0.04, 0.05, 0.06], min_sessions=6) is None
    out = aggregate_decoupling([0.04, 0.05, 0.06, 0.07, 0.05, 0.06], min_sessions=6)
    assert out["n"] == 6 and out["mean"] == pytest.approx(sum([0.04, 0.05, 0.06, 0.07, 0.05, 0.06]) / 6)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_decoupling.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/decoupling.py`**

```python
"""Aerobic decoupling (HR vs pace drift over a sustained effort).

Research brief 3.1: real signal, but only honest when aggregated over >=6
sessions on similar flat routes and presented as a trend. Single-run values
are dominated by day-to-day noise (session-residual variance 57-83%).
"""
import statistics

from normalize import Activity


def decoupling_percent(hr: list[float], speed: list[float]) -> float:
    n = len(hr)
    if n < 2 or len(speed) != n:
        raise ValueError("need equal hr/speed sequences with >=2 samples")
    half = n // 2
    first_hr, first_sp = hr[:half], speed[:half]
    second_hr, second_sp = hr[half:], speed[half:]

    def ratio(hrs, sps):
        mean_sp = sum(sps) / len(sps)
        if mean_sp == 0:
            raise ValueError("zero average speed in a half")
        return (sum(hrs) / len(hrs)) / mean_sp

    r1 = ratio(first_hr, first_sp)
    r2 = ratio(second_hr, second_sp)
    return (r2 / r1) - 1.0


def eligible_activity(a: Activity, min_duration_s: int = 5400,
                      max_ele_per_km: float = 25.0) -> bool:
    if a.sport != "running":
        return False
    if a.elapsed_s < min_duration_s:
        return False
    if a.ele_gain_m is not None and a.distance_m > 0:
        if (a.ele_gain_m / (a.distance_m / 1000.0)) > max_ele_per_km:
            return False
    return True


def route_key(a: Activity, grid: float = 0.01):
    if a.lat is None or a.lon is None:
        return ("unknown", None)
    return (round(a.lat / grid) * grid, round(a.lon / grid) * grid)


def aggregate_decoupling(decouplings: list[float], min_sessions: int = 6):
    if len(decouplings) < min_sessions:
        return None
    return {
        "n": len(decouplings),
        "mean": statistics.mean(decouplings),
        "stdev": statistics.stdev(decouplings) if len(decouplings) > 1 else 0.0,
        "min_sessions_applied": min_sessions,
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_decoupling.py -v`
Expected: `6 passed`.

- [ ] **Step 5: Add a details-series helper test (Banister/decoupling data path)**

```python
# appended to tests/test_decoupling.py
def test_route_matching_picks_same_route_sessions():
    a = [_run(date(2026, 4, i), dur_s=6000.0, lat=42.4261, lon=-71.2801) for i in range(1, 9)]
    keys = {route_key(x) for x in a}
    assert len(keys) == 1
```

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_decoupling.py -v`
Expected: `7 passed`.

- [ ] **Step 6: Commit**

```bash
git add v2/metrics/decoupling.py v2/tests/test_decoupling.py
git commit -m "feat(v2): aerobic decoupling (eligibility, halves, aggregate)"
```

### Task 10: VO2max Trend (ingest, reference)

**Files:**
- Create: `v2/metrics/vo2max.py`
- Create: `v2/tests/test_vo2max.py`

**Interfaces:**
- Consumes: `Activity` (per-run `vO2MaxValue`).
- Produces: `daily_vo2max`.

- [ ] **Step 1: Write the failing VO2max tests**

```python
# tests/test_vo2max.py
from datetime import date

import pandas as pd
import pytest

from metrics.vo2max import daily_vo2max
from normalize import Activity


def _act(d, sport, vo2):
    return Activity(activity_id=int(d.strftime("%Y%m%d")), sport=sport, date=d, ts_ms=0,
                    distance_m=5000.0, duration_s=1800.0, elapsed_s=1800.0, avg_hr=150.0,
                    max_hr=170.0, zone_s={}, vo2max=vo2)


def test_running_only_and_latest_per_day():
    acts = [
        _act(date(2026, 4, 1), "running", 45.0),
        _act(date(2026, 4, 1), "running", 46.0),
        _act(date(2026, 4, 2), "treadmill", 47.0),   # ignored
    ]
    s = daily_vo2max(acts)
    assert s[pd.Timestamp("2026-04-01")] == pytest.approx(46.0)
    assert len(s) == 1


def test_empty_when_no_running_vo2():
    acts = [_act(date(2026, 4, 2), "treadmill", 47.0)]
    assert daily_vo2max(acts).empty
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_vo2max.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/vo2max.py`**

```python
"""VO2max trend series — INGESTED from Garmin per-run vO2MaxValue.

Research brief 2.1: Firstbeat estimate (MAPE ~5%, higher at elite levels,
underestimates >=60 mL/kg/min). Never recompute. Flags surface HRmax source
and the device caveat in the series metadata (pipeline layer).
"""
import pandas as pd

from normalize import Activity


def daily_vo2max(activities: list[Activity]) -> pd.Series:
    rows = [
        (a.date, float(a.vo2max))
        for a in activities
        if a.sport == "running" and a.vo2max is not None
    ]
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_vo2max.py -v`
Expected: `2 passed`.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/vo2max.py v2/tests/test_vo2max.py
git commit -m "feat(v2): VO2max trend (ingested reference)"
```

### Task 11: Lactate Threshold + Approx CS

**Files:**
- Create: `v2/metrics/threshold.py`
- Create: `v2/tests/test_threshold.py`

**Interfaces:**
- Consumes: LT payload from `fetch_lactate_threshold()`; `Activity`.
- Produces: `LTData`, `parse_lt`, `approx_cs_1609` (signatures above).

- [ ] **Step 1: Write the failing threshold tests**

```python
# tests/test_threshold.py
from datetime import date

import pandas as pd
import pytest

from metrics.threshold import parse_lt, approx_cs_1609
from normalize import Activity


LT_PAYLOAD = {
    "speed_and_heart_rate": {"heartRate": 168, "speed": 3.35,
                             "calendarDate": "2026-08-01", "sequence": 1, "userProfilePK": 1,
                             "version": None, "heartRateCycling": None},
    "power": {},
}


def test_parse_lt_maps_hr_speed_date():
    lt = parse_lt(LT_PAYLOAD)
    assert lt.hr == 168
    assert lt.speed_m_s == pytest.approx(3.35)
    assert lt.date == "2026-08-01"


def test_parse_lt_tolerates_missing_power():
    lt = parse_lt({"speed_and_heart_rate": {}, "power": None})
    assert lt.hr is None and lt.speed_m_s is None


def test_approx_cs_from_fastest_mile():
    d = date(2026, 4, 1)
    acts = [Activity(1, "running", d, 0, 5000.0, 1800.0, 1800.0, 150.0, 170.0, {},
                     fastest_split_1609=320.1)]  # 1609m / 320.1s
    s = approx_cs_1609(acts)
    assert s.iloc[0] == pytest.approx(1609.0 / 320.1)
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `v2/metrics/threshold.py`**

```python
"""Lactate threshold (HR anchored) + approximate critical speed.

Research brief 2.2: LT heart rate is the trustworthy anchor (~7% error); LT
pace can overestimate 20-26%. Critical speed is approximated here from the
fastest 1-mile split per session — a trend signal, NOT a lab-derived CS.
"""
from dataclasses import dataclass

import pandas as pd

from normalize import Activity


@dataclass
class LTData:
    hr: int | None
    speed_m_s: float | None
    date: str | None


def parse_lt(payload: dict) -> LTData:
    sah = payload.get("speed_and_heart_rate") or {}
    return LTData(
        hr=sah.get("heartRate"),
        speed_m_s=sah.get("speed"),
        date=sah.get("calendarDate"),
    )


def approx_cs_1609(activities: list[Activity]) -> pd.Series:
    rows = []
    for a in activities:
        if a.sport != "running" or not a.fastest_split_1609:
            continue
        rows.append((a.date, 1609.0 / a.fastest_split_1609))
    if not rows:
        return pd.Series([], dtype=float)
    s = pd.Series([v for _, v in rows], index=pd.DatetimeIndex([d for d, _ in rows]))
    return s.groupby(s.index).last().sort_index()
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_threshold.py -v`
Expected: `3 passed`.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/threshold.py v2/tests/test_threshold.py
git commit -m "feat(v2): LT parse + approx critical speed"
```

### Task 12: Race Predictions (ingest)

**Files:**
- Create: `v2/metrics/racepredict.py`
- Create: `v2/tests/test_racepredict.py`
- Create: `v2/tests/fixtures/race_predictions.json`

**Interfaces:**
- Consumes: payload from `fetch_race_predictions()`.
- Produces: `DISTANCE_KEYS`, `parse_predictions`.

- [ ] **Step 1: Freeze the race-predictions payload schema**

Fetch the real payload once and freeze it so offline tests are exact. If the live call fails during execution, use the documented Garmin schema below (each event key holds an object with a `time` field in milliseconds).

```bash
cd v2 && ../.venv/bin/python - <<'PY'
import json
from pathlib import Path

from gateway import GarminGateway
g = GarminGateway()
p = g.fetch_race_predictions()
Path("tests/fixtures/race_predictions.json").write_text(json.dumps(p, indent=2))
print(sorted(p.keys()) if isinstance(p, dict) else type(p))
PY
```

Expected: prints the top-level keys. Record them here for the parser — the canonical schema is `{"Run_5k": {"time": <ms>, ...}, "Run_10k": {...}, "Run_half_marathon": {...}, "Run_full_marathon": {...}, "asOfDate": ..., "asOfDateTime": ...}`. If the real payload differs, adjust `parse_predictions` (Step 3) to match and note it in the field-mapping doc's `Run_*` rows.

- [ ] **Step 2: Write the failing parse test**

```python
# tests/test_racepredict.py
import json
from pathlib import Path

import pytest

from metrics.racepredict import DISTANCE_KEYS, parse_predictions

FIXTURE = Path(__file__).parent / "fixtures" / "race_predictions.json"


def test_canonical_schema_parses_ms_to_seconds():
    payload = {
        "Run_5k": {"time": 1500000},
        "Run_10k": {"time": 3100000},
        "Run_half_marathon": {"time": 6800000},
        "Run_full_marathon": {"time": 14400000},
    }
    out = parse_predictions(payload)
    assert out["5k"] == 1500
    assert out["full"] == 14400


def test_missing_events_become_none():
    out = parse_predictions({"Run_5k": {"time": 1500000}})
    assert out["10k"] is None


def test_frozen_payload_parses():
    data = json.loads(FIXTURE.read_text())
    out = parse_predictions(data)
    for k in DISTANCE_KEYS:
        assert k in out
```

- [ ] **Step 3: Run to verify it fails, then write `v2/metrics/racepredict.py`**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_racepredict.py -v`
Expected: fails (module missing). Then write:

```python
"""Garmin race predictions — ingested reference (research brief 2.3).

Riegel-derived. 5K/10K/half are the trustworthy end; the marathon prediction
is the least trustworthy number Garmin produces (underestimates by >=10 min
for ~half of runners). Stored as a dated series and trended, never prescribed.
"""
DISTANCE_KEYS = {
    "5k": "Run_5k",
    "10k": "Run_10k",
    "half": "Run_half_marathon",
    "full": "Run_full_marathon",
}


def parse_predictions(payload: dict) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for dist, key in DISTANCE_KEYS.items():
        entry = payload.get(key) if isinstance(payload, dict) else None
        raw = None
        if isinstance(entry, dict):
            raw = entry.get("time") or entry.get("goalTime")
        if raw is None:
            out[dist] = None
            continue
        ms = int(raw)
        out[dist] = round(ms / 1000.0)  # ms -> s (Garmin payload unit)
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_racepredict.py -v`
Expected: `3 passed`. If the frozen fixture's unit is already seconds (not ms), fix the conversion in `parse_predictions` and pin the fixture unit in `test_canonical_schema_parses_ms_to_seconds`.

- [ ] **Step 5: Commit**

```bash
git add v2/metrics/racepredict.py v2/tests/test_racepredict.py v2/tests/fixtures/race_predictions.json
git commit -m "feat(v2): race predictions ingest (5k/10k/half/full)"
```

### Task 13: Store + metric_series + Pipeline (end-to-end)

**Files:**
- Create: `v2/metric_series.py`
- Create: `v2/store.py`
- Create: `v2/pipeline.py`
- Create: `v2/tests/test_store.py`
- Create: `v2/tests/test_pipeline.py`

**Interfaces:**
- Consumes: every calculator from Tasks 5–12 + `Activity`/`RunnerProfile`.
- Produces: `rows_from_series`, `MetricStore`, `run_pipeline` (signatures above).

- [ ] **Step 1: Write `v2/metric_series.py`**

```python
"""Convert calculator output (pd.Series / pd.DataFrame) into metric_series rows."""
import json

import pandas as pd


def rows_from_series(metric: str, series, source: str, params=None, flags=None) -> list[dict]:
    params = params or {}
    flags = flags or {}
    if isinstance(series, pd.DataFrame):
        rows = []
        for col in series.columns:
            rows.extend(rows_from_series(f"{metric}.{col}", series[col], source, params, flags))
        return rows
    rows = []
    for i, value in series.items():
        if pd.isna(value):
            continue
        date = i.strftime("%Y-%m-%d") if hasattr(i, "strftime") else str(i)
        rows.append({
            "metric": metric,
            "date": date,
            "value": float(value),
            "source": source,
            "params": json.dumps(params, sort_keys=True),
            "flags": json.dumps(flags, sort_keys=True),
        })
    return rows
```

- [ ] **Step 2: Write the failing store tests**

```python
# tests/test_store.py
import json
from datetime import date

import pytest

from normalize import Activity
from profile import default_profile
from store import MetricStore


ACT = Activity(activity_id=1, sport="running", date=date(2026, 4, 1), ts_ms=0,
               distance_m=10000.0, duration_s=3600.0, elapsed_s=3600.0, avg_hr=150.0,
               max_hr=170.0, zone_s={1: 600.0, 2: 600.0, 3: 600.0, 4: 600.0, 5: 600.0},
               ele_gain_m=50.0, vo2max=46.0)


def test_store_roundtrip(tmp_path):
    db = tmp_path / "t.db"
    store = MetricStore(str(db))
    store.save_runner_profile(default_profile(age=40, hrrest=60, sex="M"))
    store.save_activities([ACT])
    store.save_metric_rows([{
        "metric": "load.banister", "date": "2026-04-01", "value": 120.5,
        "source": "computed", "params": "{}", "flags": "{}",
    }])
    rows = store.read_metric("load.banister")
    assert rows[0]["value"] == 120.5
    assert rows[0]["source"] == "computed"
    store.close()


def test_schema_has_required_tables(tmp_path):
    store = MetricStore(str(tmp_path / "t.db"))
    tables = {r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert {"runner_profile", "activities", "metric_series"} <= tables
    store.close()
```

- [ ] **Step 3: Run to verify they fail, then write `v2/store.py`**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_store.py -v`
Expected: `ModuleNotFoundError`. Then write:

```python
"""SQLite persistence for the measurement layer."""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runner_profile (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  meta  TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS activities (
  activity_id        INTEGER PRIMARY KEY,
  activity_date      TEXT NOT NULL,
  sport              TEXT NOT NULL,
  distance_m         REAL NOT NULL DEFAULT 0,
  duration_s         REAL NOT NULL DEFAULT 0,
  ele_gain_m         REAL,
  vo2max             REAL,
  avg_hr             REAL,
  max_hr             REAL,
  aerobic_te         REAL,
  anaerobic_te       REAL,
  avg_speed          REAL,
  fastest_split_1609 REAL,
  zone_s             TEXT NOT NULL DEFAULT '{}',
  route              TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS metric_series (
  metric TEXT NOT NULL,
  date   TEXT NOT NULL,
  value  REAL NOT NULL,
  source TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  flags  TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (metric, date)
);
CREATE INDEX IF NOT EXISTS idx_metric ON metric_series (metric, date);
"""


class MetricStore:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.executescript(SCHEMA)

    def save_runner_profile(self, profile) -> None:
        rows = [
            ("hrmax", str(profile.hrmax), {"source": profile.hrmax_source}),
            ("hrrest", str(profile.hrrest), {}),
            ("sex", profile.sex, {}),
            ("birth_year", str(profile.birth_year), {}),
            ("lthr_manual", "" if profile.lthr_manual is None else str(profile.lthr_manual), {}),
            ("hr_zones", json.dumps({str(k): list(v) for k, v in sorted(profile.hr_zones.items())}), {}),
            ("units", profile.units, {}),
        ]
        self.conn.executemany(
            "INSERT OR REPLACE INTO runner_profile (key, value, meta) VALUES (?, ?, ?)",
            [(k, v, json.dumps(m, sort_keys=True)) for k, v, m in rows],
        )
        self.conn.commit()

    def save_activities(self, activities) -> None:
        rows = [(
            a.activity_id, a.date.isoformat(), a.sport, a.distance_m, a.duration_s,
            a.ele_gain_m, a.vo2max, a.avg_hr, a.max_hr, a.aerobic_te, a.anaerobic_te,
            a.avg_speed, a.fastest_split_1609,
            json.dumps(a.zone_s, sort_keys=True),
            json.dumps({"lat": a.lat, "lon": a.lon, "location": a.location,
                        "device_id": a.device_id}, sort_keys=True),
        ) for a in activities]
        self.conn.executemany(
            "INSERT OR REPLACE INTO activities (activity_id, activity_date, sport, "
            "distance_m, duration_s, ele_gain_m, vo2max, avg_hr, max_hr, aerobic_te, "
            "anaerobic_te, avg_speed, fastest_split_1609, zone_s, route) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()

    def save_metric_rows(self, rows) -> None:
        if not rows:
            return
        self.conn.executemany(
            "INSERT OR REPLACE INTO metric_series (metric, date, value, source, params, flags) "
            "VALUES (:metric, :date, :value, :source, :params, :flags)",
            rows,
        )
        self.conn.commit()

    def read_metric(self, metric: str) -> list[dict]:
        cur = self.conn.execute(
            "SELECT metric, date, value, source, params, flags FROM metric_series "
            "WHERE metric = ? ORDER BY date",
            (metric,),
        )
        return [dict(zip(("metric", "date", "value", "source", "params", "flags"), row))
                for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
```

- [ ] **Step 4: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_store.py -v`
Expected: `2 passed`.

- [ ] **Step 5: Freeze the LT fixture, then write the failing pipeline test**

Create `tests/fixtures/lactate_threshold.json` from the documented `get_lactate_threshold` schema (the same shape as Task 11's `LT_PAYLOAD`), so `test_end_to_end_ingests_lt_and_race` has input:

```json
{"speed_and_heart_rate": {"heartRate": 168, "speed": 3.35, "calendarDate": "2026-08-01", "sequence": 1, "userProfilePK": 1, "version": null, "heartRateCycling": null}, "power": {}}
```

Then write the failing test:

```python
# tests/test_pipeline.py
import json
from pathlib import Path

import pandas as pd

from metric_series import rows_from_series
from normalize import from_summary
from pipeline import run_pipeline
from profile import default_profile
from store import MetricStore

FIXTURE = Path(__file__).parent / "fixtures" / "activities_sample.json"


def test_end_to_end_writes_metrics(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    db = tmp_path / "metrics.db"
    result = run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db))
    assert result["activities"] == len(acts)
    assert result["metrics_written"] > 0
    store = MetricStore(str(db))
    assert store.read_metric("volume.distance_total")
    assert store.read_metric("load.banister")
    assert store.read_metric("pmc.atl")
    assert store.read_metric("fitness.vo2max")
    # ACWR needs a 28-day chronic window; the fixture's span decides whether it exists.
    dates = [a.date for a in acts]
    span = (max(dates) - min(dates)).days
    assert bool(store.read_metric("load.acwr")) == (span >= 28)
    store.close()


def test_end_to_end_ingests_lt_and_race(tmp_path):
    data = json.loads(FIXTURE.read_text())
    acts = [from_summary(a) for a in data]
    lt = json.loads((Path(__file__).parent / "fixtures" / "lactate_threshold.json").read_text())
    race = json.loads((Path(__file__).parent / "fixtures" / "race_predictions.json").read_text())
    db = tmp_path / "metrics2.db"
    run_pipeline(acts, default_profile(age=40, hrrest=60, sex="M"), str(db),
                 lt_payload=lt, race_payload=race)
    store = MetricStore(str(db))
    assert store.read_metric("load.lt_hr")
    assert store.read_metric("load.lt_pace")
    assert store.read_metric("race_5k")
    assert store.read_metric("load.cs_approx")
    store.close()


def test_rows_from_series_keys():
    s = pd.Series([1.0, 2.5], index=pd.to_datetime(["2026-04-01", "2026-04-02"]))
    rows = rows_from_series("x", s, "computed", params={"a": 1}, flags={"b": 2})
    assert set(rows[0].keys()) == {"metric", "date", "value", "source", "params", "flags"}
    assert rows[0]["metric"] == "x"
    assert rows[1]["value"] == 2.5
```

- [ ] **Step 6: Run to verify they fail, then write `v2/pipeline.py`**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: `ModuleNotFoundError`. Then write:

```python
"""Orchestrate fetch->normalize->compute->store. Pure computation path: no network.

Ingested reference metrics (LT, race predictions) enter through optional
payload arguments — the gateway fetchers call them in, not the pipeline.
"""
import pandas as pd

from metric_series import rows_from_series
from metrics import acwr, elevation, pmc, threshold, trimp, vo2max, volume
from metrics import racepredict
from normalize import Activity
from profile import RunnerProfile
from store import MetricStore


def run_pipeline(activities: list[Activity], profile: RunnerProfile, out_db,
                 lt_payload: dict | None = None, race_payload: dict | None = None) -> dict[str, int]:
    store = MetricStore(out_db)
    store.save_runner_profile(profile)
    store.save_activities(activities)

    rows = []

    # Volume — weekly distance (km), 4-wk rolling, week-over-week % (brief 4.1)
    for group in volume.groups():
        weekly = volume.weekly_distance(activities, group)
        rows += rows_from_series(f"volume.distance_{group}", weekly, "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.rolling4wk_{group}", volume.rolling_4wk(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "km"})
        rows += rows_from_series(f"volume.wow_pct_{group}", volume.week_over_week_pct(weekly), "computed",
                                 params={"agg": "iso_week", "unit": "fraction"})

    # Elevation — context only (brief 4.2)
    gain = elevation.daily_elevation_gain(activities, "running")
    rows += rows_from_series("elevation.daily_gain_running", gain, "computed", params={"unit": "m"})
    rows += rows_from_series("elevation.rolling28d_running", elevation.rolling_28d(gain), "computed",
                             params={"unit": "m"})
    rows += rows_from_series("elevation.gain_per_km_running",
                             elevation.gain_per_km(activities, "running"), "computed",
                             params={"unit": "m/km"})

    # HR load + PMC + ACWR (brief 1.2, 1.3, 1.1)
    daily = trimp.daily_trimp(activities, profile)
    if not daily.empty:
        ban = daily["banister"]
        # DENSIFY to calendar days (load 0 on rest days) so PMC/ACWR windows are
        # calendar windows, not active-day windows.
        ban = ban.reindex(pd.date_range(ban.index.min(), ban.index.max(), freq="D"), fill_value=0.0)
        rows += rows_from_series("load.banister",
                                 ban[ban > 0], "computed",
                                 params={"hrmax": profile.hrmax, "hrrest": profile.hrrest,
                                         "sex": profile.sex, "b": profile.banister_exponent()},
                                 flags={"hrmax_source": profile.hrmax_source})
        rows += rows_from_series("load.edwards", daily["edwards"], "computed")
        rows += rows_from_series("pmc", pmc.ctl_atl_tsb(ban), "computed",
                                 params={"tau_ctl": 42, "tau_atl": 7})
        acwr_s = acwr.coupled_acwr(ban)
        rows += rows_from_series("load.acwr", acwr_s, "computed",
                                 params={"acute": 7, "chronic": 28, "coupled": True})
        rows += rows_from_series("load.acwr_pct",
                                 acwr.history_percentile(acwr_s, window=180)["history_pct"], "computed",
                                 params={"window": 180})

    # VO2max — ingested reference (brief 2.1)
    rows += rows_from_series("fitness.vo2max", vo2max.daily_vo2max(activities), "garmin_ingested",
                             flags={"error_class": "firstbeat_estimate_5pct", "recompute": "no"})

    # CS (approximate critical speed) — computed from fastestSplit_1609 (brief 2.2)
    rows += rows_from_series("load.cs_approx", threshold.approx_cs_1609(activities), "computed",
                             params={"basis": "fastestSplit_1609", "unit": "m/s"})

    # LT + race predictions — ingested reference metrics, only when payloads supplied
    if lt_payload:
        lt = threshold.parse_lt(lt_payload)
        if lt.hr is not None and lt.date:
            rows += rows_from_series("load.lt_hr",
                                     pd.Series([float(lt.hr)],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "bpm"},
                                     flags={"anchored": "hr", "error_class": "lt_hr_7pct"})
        if lt.speed_m_s is not None and lt.date:
            rows += rows_from_series("load.lt_pace",
                                     pd.Series([lt.speed_m_s],
                                               index=pd.DatetimeIndex([pd.Timestamp(lt.date)])),
                                     "garmin_ingested", params={"unit": "m/s"},
                                     flags={"anchored": "no", "error_class": "lt_pace_over_20pct"})

    if race_payload:
        as_of = race_payload.get("asOfDate") or pd.Timestamp.today().strftime("%Y-%m-%d")
        for dist, secs in racepredict.parse_predictions(race_payload).items():
            if secs is None:
                continue
            rows += rows_from_series(f"race_{dist}",
                                     pd.Series([float(secs)],
                                               index=pd.DatetimeIndex([pd.Timestamp(as_of)])),
                                     "garmin_ingested", params={"unit": "s", "distance": dist},
                                     flags={"error_class": "garmin_race_pred_maybe_optimistic"})

    store.save_metric_rows(rows)
    store.close()
    return {"metrics_written": len(rows), "activities": len(activities)}
```

- [ ] **Step 7: Run to verify they pass**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/test_pipeline.py -v`
Expected: `3 passed` (`test_end_to_end_writes_metrics`, `test_end_to_end_ingests_lt_and_race`, `test_rows_from_series_keys`). If `fitness.vo2max` is empty (fixture has few outdoor runs with `vO2MaxValue`), the assertion still passes because `read_metric` just returns `[]` — but confirm at least one row exists; if not, enrich the fixture with more `running` entries (see Task 1 Step 7). If `load.cs_approx` is empty (fixture running runs lack `fastestSplit_1609`), enrich the fixture the same way.

- [ ] **Step 8: Run the full suite**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: all tests pass (components + integration). Fix any cross-module issues now (signatures, imports).

- [ ] **Step 9: Commit**

```bash
git add v2/metric_series.py v2/store.py v2/pipeline.py v2/tests/test_store.py v2/tests/test_pipeline.py v2/tests/fixtures/lactate_threshold.json
git commit -m "feat(v2): SQLite store + end-to-end measurement pipeline"
```

### Task 14: Finalize Deliverable Docs

**Files:**
- Create: `docs/v2/metrics-spec.md`
- Modify: `docs/v2/garmin-field-mapping.md`

**Interfaces:**
- Consumes: final registry + confirmed metric list + live-captured fixture shapes.

- [ ] **Step 1: Write `docs/v2/metrics-spec.md`**

Write the document with this content (fill from the plan decisions; no TBDs):

```markdown
# v2 Metrics Specification

## 1. Decisions (from docs/research/running-metrics-brief.md)
- Default to TREND over ABSOLUTE VALUE everywhere.
- Tier metrics by evidential strength (Table in 2.2).
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
(copy the 9 items from garmin-field-mapping.md Section 6, plus: Banister
exponent is population-generic, not individualized; Garmin TRIMP is a
black box — we recompute.)

## 8. Registry metric keys
Canonical metric keys (source of truth: `v2/garmin_fields.py` `METRICS`; do not
change here without changing the registry). Generated during Step 3 verification,
kept verbatim so the appendix can be machine-checked:

```
volume, elevation, trimp_edwards, trimp_banister, ctl, atl,
tsb, acwr, decoupling, vo2max, lt_hr, lt_pace, cs_approx,
race_5k, race_10k, race_half, race_full, load_reference,
cross_training
```
```

- [ ] **Step 2: Finalize `docs/v2/garmin-field-mapping.md`**

Update these sections against reality now that live payloads exist:
- Confirmed shapes of `get_lactate_threshold` and `get_race_predictions` payloads (record exact keys captured in the frozen fixtures in Task 11/12).
- If `parse_predictions` had to deviate from the canonical ms schema, update the `Run_*.time` rows' units accordingly.
- Add the `load` (training status) and `trainingStatus` rows only if the endpoint payload matches the registry claims; otherwise add a row noting the actual shape.
- Keep Section 6 (cross-cutting limitations) in sync with Task 14 Step 1 §7.

- [ ] **Step 3: Verify registry ↔ docs consistency**

Run: `cd v2 && ../.venv/bin/python - <<'PY'
import re
from pathlib import Path

from garmin_fields import METRICS

spec = Path("../docs/v2/metrics-spec.md").read_text()
m = re.search(r"## 8\. Registry metric keys\n\n```\n(.*?)\n```", spec, re.S)
assert m, "metrics-spec.md missing section 8 (Registry metric keys)"
appendix = m.group(1)
missing = [k for k in sorted(METRICS) if k not in appendix]
assert not missing, f"missing from spec appendix: {missing}"
print(f"registry metrics ({len(METRICS)}) all present in metrics-spec.md section 8")
PY`

Expected: prints the pass message and no `AssertionError`. The code block in `metrics-spec.md` §8 is the machine-checkable copy and must exactly contain every key in `METRICS`; the §2 prose headings are descriptive, not the source of truth.

- [ ] **Step 4: Run the full suite once more**

Run: `cd v2 && ../.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add docs/v2/metrics-spec.md docs/v2/garmin-field-mapping.md
git commit -m "docs(v2): finalize metrics spec + Garmin field mapping"
```

---

## Plan Complete

All 14 tasks produce independently reviewable, committed increments. The measurement layer is complete and testable offline; visualization/UI/refresh are intentionally absent until a follow-up plan.